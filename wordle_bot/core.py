import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from io import BytesIO
from typing import Literal

import aiohttp
from nio import UploadResponse

# Configuration
WORDLE_URL = os.getenv("WORDLE_URL", "https://www.nytimes.com/svc/wordle/v2/{day}.json")
WORDLE_WORDS_URL = os.getenv("WORDLE_WORDS_URL", "https://raw.githubusercontent.com/tabatkins/wordle-list/main/words")
EMOJI_DIR = os.getenv("EMOJI_DIR", "./emojis")
EMOJI_DISPLAY_SIZE = int(os.getenv("EMOJI_DISPLAY_SIZE", "24"))

Tile = Literal["green", "yellow", "gray"]

# Runtime caches
accepted_words: set[str] | None = None
emoji_media: dict[str, str] = {}
daily_answer: tuple[date, str] | None = None


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


async def upload_emojis(client) -> None:
    """Upload required emoji assets to the homeserver and populate emoji_media.
    The client must be a nio AsyncClient instance."""
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


def guess_count_text(count: int) -> str:
    return f"{count} guess" if count == 1 else f"{count} guesses"
