import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from io import BytesIO
import os
import sys
import json
import time
from typing import Literal

import aiohttp
from dotenv import load_dotenv
from nio import (
    AsyncClient,
    AsyncClientConfig,
    MatrixRoom,
    RoomMessageText,
    LoginResponse,
    InviteMemberEvent,
    JoinError,
    LocalProtocolError,
    MegolmEvent,
    UploadResponse,
)

load_dotenv()

HOMESERVER = os.getenv("MATRIX_HOMESERVER")
USERNAME = os.getenv("MATRIX_USER")
PASSWORD = os.getenv("MATRIX_PASSWORD")
DEVICE_NAME = os.getenv("MATRIX_DEVICE_NAME", "matrix-bot")

STORE_PATH = "./store"
DEVICE_ID_FILE = "./device_id.json"
EMOJI_DIR = "./emojis"
# The bot resolves this alias and sends the result itself; users cannot edit it.
SHARE_ROOM_ALIAS = os.getenv("WORDLE_SHARE_ROOM_ALIAS", "#general:dendrite.fabianmild.dev")
WORDLE_URL = "https://www.nytimes.com/svc/wordle/v2/{day}.json"
# The accepted Wordle vocabulary used by the original NYT Wordle word list.
WORDLE_WORDS_URL = "https://raw.githubusercontent.com/tabatkins/wordle-list/main/words"
MAX_GUESSES = 6
EMOJI_DISPLAY_SIZE = 24
os.makedirs(STORE_PATH, exist_ok=True)

Tile = Literal["green", "yellow", "gray"]


@dataclass
class Guess:
    word: str
    tiles: list[Tile]
    player: str


@dataclass
class Game:
    answer: str
    guesses: list[Guess] = field(default_factory=list)
    finished: bool = False
    winner: str | None = None
    loser: str | None = None


games: dict[str, Game] = {}
daily_answer: tuple[date, str] | None = None
accepted_words: set[str] | None = None
emoji_media: dict[str, str] = {}


async def fetch_today_answer() -> str:
    """Fetch and cache the answer for the current UTC calendar day."""
    global daily_answer
    today = datetime.now(timezone.utc).date()
    if daily_answer and daily_answer[0] == today:
        return daily_answer[1]

    async with aiohttp.ClientSession() as session:
        async with session.get(WORDLE_URL.format(day=today.isoformat())) as response:
            response.raise_for_status()
            payload = await response.json()

    answer = payload.get("solution") or payload.get("answer")
    if not isinstance(answer, str) or len(answer) != 5 or not answer.isalpha():
        raise ValueError("The Wordle service returned an invalid answer.")
    daily_answer = (today, answer.lower())
    return daily_answer[1]


async def fetch_accepted_words() -> set[str]:
    global accepted_words
    if accepted_words is not None:
        return accepted_words

    async with aiohttp.ClientSession() as session:
        async with session.get(WORDLE_WORDS_URL) as response:
            response.raise_for_status()
            text = await response.text()

    words = {
        line.strip().lower()
        for line in text.splitlines()
        if len(line.strip()) == 5 and line.strip().isalpha()
    }
    if not words:
        raise ValueError("The Wordle vocabulary was empty.")
    accepted_words = words
    return words


async def upload_emojis() -> None:
    required = [f"{letter}_{state}.png" for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" for state in ("gray", "yellow", "green")]
    required.append("empty_gray.png")
    missing = [name for name in required if not os.path.isfile(os.path.join(EMOJI_DIR, name))]
    if missing:
        raise FileNotFoundError(f"Missing emoji assets: {', '.join(missing)}")

    for filename in required:
        path = os.path.join(EMOJI_DIR, filename)
        with open(path, "rb") as image_file:
            response, _ = await client.upload(
                BytesIO(image_file.read()),
                content_type="image/png",
                filename=filename,
            )
        if not isinstance(response, UploadResponse):
            raise RuntimeError(f"Could not upload emoji asset {filename}: {response}")
        stem = filename.removesuffix(".png")
        name = f":{stem}:"
        emoji_media[name] = response.content_uri


def score_guess(answer: str, guess: str) -> list[Tile]:
    """Score a guess using Wordle's duplicate-letter rules."""
    tiles: list[Tile] = ["gray"] * 5
    remaining: dict[str, int] = {}
    for index, letter in enumerate(answer):
        if guess[index] == letter:
            tiles[index] = "green"
        else:
            remaining[letter] = remaining.get(letter, 0) + 1
    for index, letter in enumerate(guess):
        if tiles[index] == "green":
            continue
        if remaining.get(letter, 0):
            tiles[index] = "yellow"
            remaining[letter] -= 1
    return tiles


def emoji_name(letter: str, state: Tile) -> str:
    return f":{letter.upper()}_{state}:"


def emoji_html(letter: str, state: Tile) -> str:
    name = emoji_name(letter, state)
    uri = emoji_media.get(name)
    if not uri:
        return name
    return (
        f'<img src="{uri}" alt="{name}" data-mx-emoticon="" '
        f'width="{EMOJI_DISPLAY_SIZE}" height="{EMOJI_DISPLAY_SIZE}"/>'
    )


def empty_emoji_html() -> str:
    name = ":empty_gray:"
    uri = emoji_media.get(name)
    if not uri:
        return name
    return (
        f'<img src="{uri}" alt="{name}" data-mx-emoticon="" '
        f'width="{EMOJI_DISPLAY_SIZE}" height="{EMOJI_DISPLAY_SIZE}"/>'
    )


def render_grid(game: Game) -> str:
    return "\n".join(
        "".join(emoji_name(letter, state) for letter, state in zip(guess.word, guess.tiles))
        for guess in game.guesses
    )


def render_html_grid(game: Game) -> str:
    return "<br>".join(
        "".join(emoji_html(letter, state) for letter, state in zip(guess.word, guess.tiles))
        for guess in game.guesses
    )


def render_share_message(game: Game, comment: str = "") -> tuple[str, str]:
    if game.winner:
        result = f"{game.winner} won today's Wordle in {guess_count_text(len(game.guesses))}!"
    else:
        result = f"Imagine failing today's Wordle, {game.loser or 'player'}"
    grid = render_grid(game)
    if comment:
        result += f' - "{comment}"'
    html_result = result.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html_result = html_result.replace('"', "&quot;")
    return f"{result}\n\n{grid}", f"{html_result}<br><br>{render_html_grid(game)}"


def load_device_id():
    if os.path.exists(DEVICE_ID_FILE):
        with open(DEVICE_ID_FILE) as f:
            return json.load(f).get("device_id")
    return None


def save_device_id(device_id):
    with open(DEVICE_ID_FILE, "w") as f:
        json.dump({"device_id": device_id}, f)


async def message_callback(room: MatrixRoom, event: RoomMessageText):
    if event.sender == client.user_id:
        return
    print(f"[{room.display_name}] {event.sender}: {event.body}")
    if event.body.startswith("!touch"):
        await send(room.room_id, f"Hello {event.sender.split(':')[0]}, it is currently {time.ctime(time.time())}. Have a nice day!")
    elif event.body.lower().startswith("!guess"):
        await handle_guess(room.room_id, event.sender, event.body)
    elif event.body.strip().lower() == "!share" or event.body.strip().lower().startswith("!share "):
        await share_game(room.room_id, event.body[len("!share"):].strip())


async def handle_guess(room_id: str, sender: str, body: str):
    parts = body.split()
    if len(parts) != 2 or len(parts[1]) != 5 or not parts[1].isalpha():
        await send(room_id, "A guess must be exactly five letters: `!guess ABCDE`.")
        return

    game = games.get(room_id)
    if not game:
        game = await create_game(room_id)
        if not game:
            return
        games[room_id] = game

    if game.finished:
        await send(room_id, "This room's Wordle is already over.")
        return

    word = parts[1].lower()
    try:
        words = await fetch_accepted_words()
    except (aiohttp.ClientError, ValueError) as error:
        print(f"Could not fetch the Wordle vocabulary: {error}")
        await send(room_id, "I couldn't validate that guess right now. Please try again later.")
        return
    if word not in words:
        await send(room_id, f"`{word.upper()}` is not an accepted Wordle word.")
        return

    guess = Guess(word=word, tiles=score_guess(game.answer, word), player=sender)
    game.guesses.append(guess)
    if word == game.answer:
        game.finished = True
        game.winner = sender.split(":", 1)[0].lstrip("@")
        await send_completion(room_id, f"{game.winner} won in {guess_count_text(len(game.guesses))}!", game)
        await share_game(room_id)
    elif len(game.guesses) == MAX_GUESSES:
        game.finished = True
        game.loser = sender.split(":", 1)[0].lstrip("@")
        await send_completion(room_id, f"Imagine failing today's Wordle, {game.loser}", game)
        await share_game(room_id)
    else:
        await send(
            room_id,
            f"{render_grid(game)}\n\n{len(game.guesses)}/{MAX_GUESSES} guesses used.",
            f"{render_html_grid(game)}<br><br>{len(game.guesses)}/{MAX_GUESSES} guesses used.",
        )


async def create_game(room_id: str) -> Game | None:
    try:
        answer = await fetch_today_answer()
        words = await fetch_accepted_words()
    except (aiohttp.ClientError, ValueError, json.JSONDecodeError) as error:
        print(f"Could not fetch today's Wordle: {error}")
        await send(room_id, "I couldn't fetch today's Wordle right now. Please try again later.")
        return None
    if answer not in words:
        print("Today's answer was not present in the accepted Wordle vocabulary.")
        await send(room_id, "Today's Wordle data could not be validated. Please try again later.")
        return None
    return Game(answer=answer)


def guess_count_text(count: int) -> str:
    return f"{count} guess" if count == 1 else f"{count} guesses"


async def send_completion(room_id: str, text: str, game: Game):
    await send(
        room_id,
        f"{render_grid(game)}\n\n{text}\nThe result was shared to {SHARE_ROOM_ALIAS}.",
        f"{render_html_grid(game)}<br><br>{text}<br>"
        f"The result was shared to {SHARE_ROOM_ALIAS}.",
    )


async def share_game(room_id: str, comment: str = ""):
    game = games.get(room_id)
    if not game or not game.finished:
        await send(room_id, "Finish today's Wordle before sharing the result.")
        return
    comment = comment.replace("\n", " ").strip()
    plain, html = render_share_message(game, comment)
    try:
        response = await client.room_resolve_alias(SHARE_ROOM_ALIAS)
        target_room_id = response.room_id
        await client.room_send(
            room_id=target_room_id,
            message_type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": plain,
                "format": "org.matrix.custom.html",
                "formatted_body": html,
            },
            ignore_unverified_devices=True,
        )
    except (LocalProtocolError, aiohttp.ClientError, ValueError) as error:
        print(f"Share failed for {SHARE_ROOM_ALIAS}: {error}")
        await send(room_id, "I couldn't share the result to the configured share room.")
        return
    await send(room_id, f"Result shared to {SHARE_ROOM_ALIAS}.")


async def send(room_id, text, formatted_body=None):
    try:
        content = {"msgtype": "m.text", "body": text}
        if formatted_body is not None:
            content.update({"format": "org.matrix.custom.html", "formatted_body": formatted_body})
        await client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content=content,
            ignore_unverified_devices=True,
        )
    except LocalProtocolError as e:
        print(f"Send failed (encryption issue): {e}")


async def invite_callback(room: MatrixRoom, event: InviteMemberEvent):
    url = f"{client.homeserver}/_matrix/client/v3/join/{room.room_id}"
    headers = {"Authorization": f"Bearer {client.access_token}"}
    async with client.client_session.post(url, data=json.dumps({}), headers=headers) as resp:
        text = await resp.text()
        print(f"Join response: {resp.status} {text}")




async def undecrypted_callback(room, event: MegolmEvent):
    print(f"Could not decrypt {event.event_id} in {room.room_id} — requesting key")
    try:
        await client.request_room_key(event)
    except Exception as e:
        print(f"Key request note: {e}")


async def main():
    global client

    config = AsyncClientConfig(
        store_sync_tokens=True,
        encryption_enabled=True,
    )

    saved_device_id = load_device_id()

    client = AsyncClient(
        HOMESERVER,
        USERNAME,
        store_path=STORE_PATH,
        config=config,
        device_id=saved_device_id,
    )

    resp = await client.login(PASSWORD, device_name=DEVICE_NAME)
    if not isinstance(resp, LoginResponse):
        print(f"Login failed: {resp}")
        sys.exit(1)

    print(f"Logged in as {client.user_id}, device {client.device_id}")
    save_device_id(client.device_id)

    if client.should_upload_keys:
        await client.keys_upload()

    try:
        await upload_emojis()
    except (OSError, RuntimeError) as error:
        print(f"Emoji upload failed: {error}")
        sys.exit(1)

    client.add_event_callback(message_callback, RoomMessageText)
    client.add_event_callback(invite_callback, InviteMemberEvent)
    client.add_event_callback(undecrypted_callback, MegolmEvent)

    await client.sync_forever(timeout=50000, full_state=True)


if __name__ == "__main__":
    asyncio.run(main())
