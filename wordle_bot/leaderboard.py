import os
import sqlite3
from datetime import datetime, timezone, date

STORE_PATH = os.getenv("STORE_PATH", "./store")
DB_PATH = os.path.join(STORE_PATH, "leaderboard.db")


def _normalize_user(sender: str) -> str:
    return sender.split(":", 1)[0].lstrip("@")


def _init_db():
    os.makedirs(STORE_PATH, exist_ok=True)
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


def reset_database() -> str:
    """Reset the entire database and return a summary of what was deleted."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    # Get all players and their stats before deletion
    cur.execute("SELECT * FROM players")
    rows = cur.fetchall()
    players_data = [_row_to_dict(cur, row) for row in rows]
    
    # Delete all data
    cur.execute("DELETE FROM weekly_stats")
    cur.execute("DELETE FROM players")
    conn.commit()
    conn.close()
    
    # Format summary
    summary = f"Database reset. Deleted {len(players_data)} players:\n"
    for player in players_data:
        wins = player.get("total_wins", 0) or 0
        games = player.get("total_games", 0) or 0
        summary += f"  - {player['name']}: {wins}/{games} wins\n"
    
    return summary


def set_player_stats(player_id: str, global_wins: int, global_games: int, weekly_wins: int = 0, weekly_games: int = 0) -> str:
    """Set player stats directly. Returns status message."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    player = _get_or_create_player(conn, player_id)
    pid = player["id"]
    
    global_losses = global_games - global_wins
    
    # Update global stats
    cur.execute(
        "UPDATE players SET total_wins=?, total_losses=?, total_games=? WHERE id=?",
        (global_wins, global_losses, global_games, pid),
    )
    
    # Update weekly stats if provided
    if weekly_games > 0:
        year, week, _ = datetime.now(timezone.utc).isocalendar()
        week_id = f"{year}-{week:02d}"
        weekly_losses = weekly_games - weekly_wins
        
        cur.execute("SELECT * FROM weekly_stats WHERE player_id=? AND week_id=?", (pid, week_id))
        if cur.fetchone():
            cur.execute(
                "UPDATE weekly_stats SET wins=?, losses=? WHERE player_id=? AND week_id=?",
                (weekly_wins, weekly_losses, pid, week_id),
            )
        else:
            cur.execute(
                "INSERT INTO weekly_stats(player_id, week_id, wins, losses) VALUES(?,?,?,?)",
                (pid, week_id, weekly_wins, weekly_losses),
            )
    
    conn.commit()
    conn.close()
    
    return f"Updated {player_id}: {global_wins}/{global_games} global wins, {weekly_wins}/{weekly_games} weekly wins"


# initialize DB on import
_init_db()
