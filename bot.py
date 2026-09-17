"""Entrypoint for the Matrix Wordle bot.

This file acts as a thin orchestration layer. Core game logic and storage
have been moved into the wordle_bot package.
"""

import asyncio
import os
import sys
import json
import time
import html
from datetime import datetime, timezone

from dotenv import load_dotenv
from nio import (
    AsyncClient,
    AsyncClientConfig,
    MatrixRoom,
    RoomMessageText,
    LoginResponse,
    InviteMemberEvent,
    LocalProtocolError,
    MegolmEvent,
)

from wordle_bot import core
from wordle_bot import leaderboard

load_dotenv()

HOMESERVER = os.getenv("MATRIX_HOMESERVER")
USERNAME = os.getenv("MATRIX_USER")
PASSWORD = os.getenv("MATRIX_PASSWORD")
DEVICE_NAME = os.getenv("MATRIX_DEVICE_NAME", "matrix-bot")
STORE_PATH = os.getenv("STORE_PATH", "./store")
DEVICE_ID_FILE = os.getenv("DEVICE_ID_FILE", "./device_id.json")
SHARE_ROOM_ALIAS = os.getenv("WORDLE_SHARE_ROOM_ALIAS", "#general:dendrite.fabianmild.dev")
MAX_GUESSES = int(os.getenv("MAX_GUESSES", "6"))

# runtime state
client = None
games: dict[str, core.Game] = {}


def load_device_id():
    if os.path.exists(DEVICE_ID_FILE):
        with open(DEVICE_ID_FILE) as f:
            return json.load(f).get("device_id")
    return None


def save_device_id(device_id):
    with open(DEVICE_ID_FILE, "w") as f:
        json.dump({"device_id": device_id}, f)


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


async def show_leaderboard(room_id: str, sender: str):
    # reuse the previous display logic but query the leaderboard package for data
    user = leaderboard._normalize_user(sender)
    players = leaderboard.get_leaderboard_sorted()
    if not players:
        await send(room_id, "No leaderboard data yet.")
        return

    rows_html = []
    rows_text = []
    rows_html.append("<tr><th>Rank</th><th>Player</th><th>🌍 all time</th><th>⏱️ this week</th><th>⚡ streak</th><th>📈 highest streak</th></tr>")

    def format_for_text(rank: int, name: str, all_cell: str, week_cell: str, current: int, best: int) -> str:
        return f"{rank}. {name} | {all_cell} | {week_cell} | {current} | {best}"

    year, week, _ = datetime.now(timezone.utc).isocalendar()
    week_id = f"{year}-{week:02d}"

    for idx, (name, rate, tg, p) in enumerate(players[:3], start=1):
        wins = p.get("total_wins", 0) or 0
        games = tg or 0
        # get weekly stats
        conn = __import__("sqlite3").connect(os.path.join(STORE_PATH, "leaderboard.db"))
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

    # optionally show user's rank when not in top 3
    user_rank = None
    user_data = None
    for idx, (name, rate, tg, p) in enumerate(players, start=1):
        if name == user:
            user_rank = idx
            user_data = (name, rate, tg, p)
            break

    if user_rank is None:
        if len(players) > 3:
            rows_html.append("<tr><td colspan=7>—</td></tr>")
            rows_text.append("-----")
            rows_text.append(f"{user} — No finished games yet.")
            rows_html.append(f"<tr><td colspan=7>{html.escape(user + ' — No finished games yet.')}</td></tr>")
    elif user_rank > 3:
        name, rate, tg, p = user_data
        wins = p.get("total_wins", 0) or 0
        games = p.get("total_games", 0) or 0
        conn = __import__("sqlite3").connect(os.path.join(STORE_PATH, "leaderboard.db"))
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


async def message_callback(room: MatrixRoom, event: RoomMessageText):
    if event.sender == client.user_id:
        return

    today = datetime.now(timezone.utc).date()
    # reset per-day state when day changes
    if core.daily_answer and core.daily_answer[0] != today:
        core.daily_answer = None
        games.clear()

    print(f"[{room.display_name}] {event.sender}: {event.body}")

    body = event.body.strip()
    if body.startswith("!touch"):
        await send(room.room_id, f"Hello {event.sender.split(':')[0]}, it is currently {time.ctime(time.time())}. Have a nice day!")
    elif body.lower().startswith("!guess"):
        await handle_guess(room.room_id, event.sender, body)
    elif body.lower() == "!leaderboard":
        await show_leaderboard(room.room_id, event.sender)
    elif body.lower().startswith("!share"):
        await share_game(room.room_id, body[len("!share"):].strip())


async def handle_guess(room_id: str, sender: str, body: str):
    parts = body.split()
    if len(parts) != 2 or len(parts[1]) != 5 or not parts[1].isalpha():
        await send(room_id, "A guess must be exactly five letters: `!guess ABCDE`.")
        return

    game = games.get(room_id)
    if not game:
        try:
            answer = await core.fetch_today_answer()
            words = await core.fetch_accepted_words()
        except Exception as e:
            print(f"Could not fetch today's word data: {e}")
            await send(room_id, "I couldn't fetch today's Wordle right now. Please try again later.")
            return
        if answer not in words:
            await send(room_id, "Today's Wordle data could not be validated. Please try again later.")
            return
        game = core.Game(answer=answer)
        games[room_id] = game

    if game.finished:
        await send(room_id, "This room's Wordle is already over.")
        return

    word = parts[1].lower()
    words = await core.fetch_accepted_words()
    if word not in words:
        await send(room_id, f"`{word.upper()}` is not an accepted Wordle word.")
        return

    guess = core.Guess(word=word, tiles=core.score_guess(game.answer, word), player=sender)
    game.guesses.append(guess)

    if word == game.answer:
        game.finished = True
        game.winner = sender.split(":", 1)[0].lstrip("@")
        await send_completion(room_id, f"{game.winner} won in {core.guess_count_text(len(game.guesses))}!", game)
    elif len(game.guesses) == MAX_GUESSES:
        game.finished = True
        game.loser = sender.split(":", 1)[0].lstrip("@")
        await send_completion(room_id, f"Imagine failing today's Wordle, {game.loser}", game)
    else:
        await send(
            room_id,
            f"{core.render_grid(game)}\n\n{len(game.guesses)}/{MAX_GUESSES} guesses used.",
            f"{core.render_html_grid(game)}<br><br>{len(game.guesses)}/{MAX_GUESSES} guesses used.",
        )


async def send_completion(room_id: str, text: str, game: core.Game):
    finished_date = datetime.now(timezone.utc).date()
    if game.winner:
        leaderboard.update_leaderboard_for_player(game.winner, True, finished_date)
    elif game.loser:
        leaderboard.update_leaderboard_for_player(game.loser, False, finished_date)

    await send(
        room_id,
        f"{core.render_grid(game)}\n\n{text}",
        f"{core.render_html_grid(game)}<br><br>{text}",
    )


async def share_game(room_id: str, comment: str = ""):
    game = games.get(room_id)
    if not game or not game.finished:
        await send(room_id, "Finish today's Wordle before sharing the result.")
        return

    try:
        response = await client.room_resolve_alias(SHARE_ROOM_ALIAS)
        target_room_id = response.room_id
    except Exception as error:
        print(f"Could not resolve {SHARE_ROOM_ALIAS}: {error}")
        await send(room_id, "I couldn't resolve the share room.")
        return

    if room_id == target_room_id:
        await send(room_id, "You cannot share the result from the share room itself.")
        return

    comment = comment.replace("\n", " ").strip()
    plain, html_body = core.render_share_message(game, comment)
    try:
        await client.room_send(
            room_id=target_room_id,
            message_type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": plain,
                "format": "org.matrix.custom.html",
                "formatted_body": html_body,
            },
            ignore_unverified_devices=True,
        )
    except Exception as error:
        print(f"Share failed for {SHARE_ROOM_ALIAS}: {error}")
        await send(room_id, "I couldn't share the result to the configured share room.")
        return
    await send(room_id, f"Result shared to {SHARE_ROOM_ALIAS}.")


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
        # upload emoji assets via the core module (uses client)
        await core.upload_emojis(client)
    except (OSError, RuntimeError) as error:
        print(f"Emoji upload failed: {error}")
        sys.exit(1)

    client.add_event_callback(message_callback, RoomMessageText)
    client.add_event_callback(invite_callback, InviteMemberEvent)
    client.add_event_callback(undecrypted_callback, MegolmEvent)

    await client.sync_forever(timeout=50000, full_state=True)


if __name__ == "__main__":
    asyncio.run(main())
