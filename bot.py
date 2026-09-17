import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from io import BytesIO
import os
import sys
import json
import sqlite3
import time
import html
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


async def fetch_today_answer(force_refresh: bool = False) -> str:
    """Fetch and cache the answer for the current UTC calendar day."""
    global daily_answer
    today = datetime.now(timezone.utc).date()
    if not force_refresh and daily_answer and daily_answer[0] == today:
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
    required.extend(["empty_gray.png", "empty_yellow.png", "empty_green.png"])
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


def empty_tile_emoji_name(state: Tile) -> str:
    return f":empty_{state}:"


def empty_tile_emoji_html(state: Tile) -> str:
    name = empty_tile_emoji_name(state)
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


def render_share_grid(game: Game) -> str:
    return "\n".join(
        "".join(empty_tile_emoji_name(state) for state in guess.tiles)
        for guess in game.guesses
    )


def render_share_html_grid(game: Game) -> str:
    return "<br>".join(
        "".join(empty_tile_emoji_html(state) for state in guess.tiles)
        for guess in game.guesses
    )


def render_share_message(game: Game, comment: str = "") -> tuple[str, str]:
    if game.winner:
        result = f"{game.winner} won today's Wordle in {guess_count_text(len(game.guesses))}!"
    else:
        result = f"Imagine failing today's Wordle, {game.loser or 'player'}"
    grid = render_share_grid(game)
    if comment:
        result += f' - "{comment}"'
    html_result = result.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html_result = html_result.replace('"', "&quot;")
    return f"{result}\n\n{grid}", f"{html_result}<br><br>{render_share_html_grid(game)}"


def load_device_id():
    if os.path.exists(DEVICE_ID_FILE):
        with open(DEVICE_ID_FILE) as f:
            return json.load(f).get("device_id")
    return None


def save_device_id(device_id):
    with open(DEVICE_ID_FILE, "w") as f:
        json.dump({"device_id": device_id}, f)


# Leaderboard storage and helper functions (uses SQLite)
DB_PATH = os.path.join(STORE_PATH, "leaderboard.db")


def _normalize_user(sender: str) -> str:
    return sender.split(":", 1)[0].lstrip("@")


def _init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY,
            name TEXT UNIQUE,
            total_wins INTEGER DEFAULT 0,
            total_losses INTEGER DEFAULT 0,
            total_games INTEGER DEFAULT 0,
            current_streak INTEGER DEFAULT 0,
            highest_streak INTEGER DEFAULT 0,
            last_finished_date TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS weekly_stats (
            player_id INTEGER,
            week_id TEXT,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            PRIMARY KEY(player_id, week_id),
            FOREIGN KEY(player_id) REFERENCES players(id)
        )
        """
    )
    conn.commit()
    conn.close()


def _row_to_dict(cur, row):
    if not row:
        return None
    return {desc[0]: row[idx] for idx, desc in enumerate(cur.description)}


def _get_or_create_player(conn, name: str) -> dict:
    cur = conn.cursor()
    cur.execute("SELECT * FROM players WHERE name=?", (name,))
    row = cur.fetchone()
    if not row:
        cur.execute("INSERT INTO players(name) VALUES(?)", (name,))
        conn.commit()
        cur.execute("SELECT * FROM players WHERE name=?", (name,))
        row = cur.fetchone()
    return _row_to_dict(cur, row)


def update_leaderboard_for_player(player: str, won: bool, finished_date: date):
    name = player
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    p = _get_or_create_player(conn, name)
    pid = p["id"]
    total_wins = p.get("total_wins", 0) or 0
    total_losses = p.get("total_losses", 0) or 0
    current_streak = p.get("current_streak", 0) or 0
    highest_streak = p.get("highest_streak", 0) or 0
    last_finished = p.get("last_finished_date")

    if won:
        total_wins += 1
    else:
        total_losses += 1
    total_games = total_wins + total_losses

    # weekly
    year, week, _ = finished_date.isocalendar()
    week_id = f"{year}-{week:02d}"
    cur.execute("SELECT wins, losses FROM weekly_stats WHERE player_id=? AND week_id=?", (pid, week_id))
    wk = cur.fetchone()
    if wk:
        ww, wl = wk
        ww = ww + 1 if won else ww
        wl = wl + 1 if not won else wl
        cur.execute("UPDATE weekly_stats SET wins=?, losses=? WHERE player_id=? AND week_id=?", (ww, wl, pid, week_id))
    else:
        ww = 1 if won else 0
        wl = 0 if won else 1
        cur.execute("INSERT INTO weekly_stats(player_id, week_id, wins, losses) VALUES(?,?,?,?)", (pid, week_id, ww, wl))

    # streaks: only count one finished game per calendar day
    if last_finished:
        try:
            last_date = datetime.fromisoformat(last_finished).date()
        except Exception:
            last_date = None
    else:
        last_date = None

    if last_date == finished_date:
        # already recorded today; do not change streak counts
        pass
    else:
        if last_date and (finished_date - last_date).days == 1:
            current_streak = (current_streak or 0) + 1
        else:
            current_streak = 1
        if current_streak > (highest_streak or 0):
            highest_streak = current_streak

    cur.execute(
        "UPDATE players SET total_wins=?, total_losses=?, total_games=?, current_streak=?, highest_streak=?, last_finished_date=? WHERE id=?",
        (total_wins, total_losses, total_games, current_streak, highest_streak, finished_date.isoformat(), pid),
    )

    conn.commit()
    conn.close()


def get_player_stats(player: str) -> dict:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT * FROM players WHERE name=?", (player,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return {
            "total_wins": 0,
            "total_losses": 0,
            "total_games": 0,
            "current_streak": 0,
            "highest_streak": 0,
            "last_finished_date": None,
            "weekly": {},
        }
    p = _row_to_dict(cur, row)
    # load current week
    year, week, _ = datetime.now(timezone.utc).isocalendar()
    week_id = f"{year}-{week:02d}"
    cur.execute("SELECT wins, losses FROM weekly_stats WHERE player_id=? AND week_id=?", (p["id"], week_id))
    wk = cur.fetchone()
    if wk:
        p["weekly"] = {week_id: {"wins": wk[0], "losses": wk[1]}}
    else:
        p["weekly"] = {}
    conn.close()
    return p


def get_leaderboard_sorted() -> list:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT * FROM players")
    rows = cur.fetchall()
    players = []
    for row in rows:
        p = _row_to_dict(cur, row)
        tg = p.get("total_games", 0) or 0
        wins = p.get("total_wins", 0) or 0
        win_rate = (wins / tg) if tg > 0 else 0
        players.append((p["name"], win_rate, tg, p))
    conn.close()
    players.sort(key=lambda x: (-x[1], -x[2], -x[3].get("highest_streak", 0)))
    return players


async def show_leaderboard(room_id: str, sender: str):
    user = _normalize_user(sender)
    players = get_leaderboard_sorted()
    if not players:
        await send(room_id, "No leaderboard data yet.")
        return

    # build HTML table
    rows_html = []
    rows_text = []

    # header
    rows_html.append(
        "<tr>"
        "<th>Rank</th><th>Player</th>"
        "<th>🌍 all time</th><th>⏱️ this week</th><th>⚡ streak</th><th>📈 highest streak</th>"
        "</tr>"
    )

    def format_for_text(rank: int, name: str, all_cell: str, week_cell: str, current: int, best: int) -> str:
        return f"{rank}. {name} | {all_cell} | {week_cell} | {current} | {best}"

    # helper to get week cells
    year, week, _ = datetime.now(timezone.utc).isocalendar()
    week_id = f"{year}-{week:02d}"

    # top 3
    for idx, (name, rate, tg, p) in enumerate(players[:3], start=1):
        wins = p.get("total_wins", 0) or 0
        games = tg or 0
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            "SELECT wins, losses FROM weekly_stats ws JOIN players pl ON ws.player_id=pl.id WHERE pl.name=? AND ws.week_id=?",
            (name, week_id),
        )
        wk = cur.fetchone()
        conn.close()
        ww, wl = (wk if wk else (0, 0))
        wgames = ww + wl
        all_rate = (wins / games) if games > 0 else 0
        week_rate = (ww / wgames) if wgames > 0 else 0
        all_cell = f"{wins}/{games} ({all_rate:.0%})"
        week_cell = f"{ww}/{wgames} ({week_rate:.0%})"
        rows_html.append(
            f"<tr><td>{idx}</td><td>{html.escape(name)}</td><td>{html.escape(all_cell)}</td><td>{html.escape(week_cell)}</td><td>{p.get('current_streak',0) or 0}</td><td>{p.get('highest_streak',0) or 0}</td></tr>"
        )
        rows_text.append(format_for_text(idx, name, all_cell, week_cell, p.get("current_streak", 0) or 0, p.get("highest_streak", 0) or 0))

    # find user's rank
    user_rank = None
    user_data = None
    for idx, (name, rate, tg, p) in enumerate(players, start=1):
        if name == user:
            user_rank = idx
            user_data = (name, rate, tg, p)
            break

    if user_rank is None:
        if len(players) > 3:
            # divider row
            rows_html.append("<tr><td colspan=7>—</td></tr>")
            rows_text.append("-----")
            rows_text.append(f"{user} — No finished games yet.")
            rows_html.append(f"<tr><td colspan=7>{html.escape(user + ' — No finished games yet.')}</td></tr>")
    elif user_rank > 3:
        name, rate, tg, p = user_data
        wins = p.get("total_wins", 0) or 0
        games = p.get("total_games", 0) or 0
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            "SELECT wins, losses FROM weekly_stats ws JOIN players pl ON ws.player_id=pl.id WHERE pl.name=? AND ws.week_id=?",
            (name, week_id),
        )
        wk = cur.fetchone()
        conn.close()
        ww, wl = (wk if wk else (0, 0))
        wgames = ww + wl
        all_rate = (wins / games) if games > 0 else 0
        week_rate = (ww / wgames) if wgames > 0 else 0
        all_cell = f"{all_rate:.0%} ({wins}/{games})"
        week_cell = f"{week_rate:.0%} ({ww}/{wgames})"
        rows_html.append("<tr><td colspan=7>—</td></tr>")
        rows_html.append(
            f"<tr><td>{user_rank}</td><td>{html.escape(name)}</td><td>{html.escape(all_cell)}</td><td>{html.escape(week_cell)}</td><td>{games}</td><td>{p.get('current_streak',0) or 0}</td><td>{p.get('highest_streak',0) or 0}</td></tr>"
        )
        rows_text.append("-----")
        rows_text.append(format_for_text(user_rank, name, all_cell, week_cell, games, p.get("current_streak", 0) or 0, p.get("highest_streak", 0) or 0))

    html_table = "<table border=1 cellpadding=4 cellspacing=0>" + "".join(rows_html) + "</table>"
    plain_text = "\n".join(["Rank. Player | 🌍 all time | ⏱️ this week | # games | ⚡ streak | 📈 highest streak"] + rows_text)

    await send(room_id, plain_text, html_table)

# initialize DB
_init_db()


async def message_callback(room: MatrixRoom, event: RoomMessageText):
    if event.sender == client.user_id:
        return
    
    # Check if the day has changed and refresh the daily answer if needed
    global daily_answer
    today = datetime.now(timezone.utc).date()
    if daily_answer and daily_answer[0] != today:
        daily_answer = None
        games.clear()
    
    print(f"[{room.display_name}] {event.sender}: {event.body}")
    if event.body.startswith("!touch"):
        await send(room.room_id, f"Hello {event.sender.split(':')[0]}, it is currently {time.ctime(time.time())}. Have a nice day!")
    elif event.body.lower().startswith("!guess"):
        await handle_guess(room.room_id, event.sender, event.body)
    elif event.body.strip().lower() == "!leaderboard":
        await show_leaderboard(room.room_id, event.sender)
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
    elif len(game.guesses) == MAX_GUESSES:
        game.finished = True
        game.loser = sender.split(":", 1)[0].lstrip("@")
        await send_completion(room_id, f"Imagine failing today's Wordle, {game.loser}", game)
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
    # Update leaderboard for the finished player (winner or loser). Use UTC date.
    finished_date = datetime.now(timezone.utc).date()
    if game.winner:
        # game.winner is stored as user localpart (no @ or domain)
        update_leaderboard_for_player(game.winner, True, finished_date)
    elif game.loser:
        update_leaderboard_for_player(game.loser, False, finished_date)

    await send(
        room_id,
        f"{render_grid(game)}\n\n{text}",
        f"{render_html_grid(game)}<br><br>{text}",
    )


async def share_game(room_id: str, comment: str = ""):
    game = games.get(room_id)
    if not game or not game.finished:
        await send(room_id, "Finish today's Wordle before sharing the result.")
        return
    
    try:
        response = await client.room_resolve_alias(SHARE_ROOM_ALIAS)
        target_room_id = response.room_id
    except (LocalProtocolError, aiohttp.ClientError, ValueError) as error:
        print(f"Could not resolve {SHARE_ROOM_ALIAS}: {error}")
        await send(room_id, "I couldn't resolve the share room.")
        return
    
    if room_id == target_room_id:
        await send(room_id, "You cannot share the result from the share room itself.")
        return
    
    comment = comment.replace("\n", " ").strip()
    plain, html = render_share_message(game, comment)
    try:
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
