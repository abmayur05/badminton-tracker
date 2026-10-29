"""
Badminton Tracker's persistence layer -- SQLite (via libSQL), shared by the
website and the MCP server. Tracks personal badminton play: sessions (date,
venue, duration), the matches played in each session, and the per-game scores.

Storage:
    TURSO_DATABASE_URL set -> remote Turso database (used on Vercel)
    otherwise              -> local file badminton.db (or BADMINTON_DB_PATH)

Errors:
    ValueError  -> invalid input (REST returns 400)
    LookupError -> unknown id     (REST returns 404)
"""
import os
from datetime import date
from pathlib import Path

import libsql

DB_PATH = Path(os.environ.get("BADMINTON_DB_PATH", Path(__file__).parent / "badminton.db"))

ALLOWED_POINT_TARGETS = (11, 15, 21)
DEFAULT_POINTS_TO_WIN = 21
CAP_MARGIN = 9  # 21 -> 30, 15 -> 24, 11 -> 20
MAX_SESSION_MINUTES = 24 * 60

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_date TEXT NOT NULL,
        venue TEXT NOT NULL,
        duration_minutes INTEGER NOT NULL,
        notes TEXT NOT NULL DEFAULT '',
        is_sample INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS matches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL REFERENCES sessions(id),
        points_to_win INTEGER NOT NULL DEFAULT 21,
        won INTEGER NOT NULL,
        notes TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS games (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id INTEGER NOT NULL REFERENCES matches(id),
        game_no INTEGER NOT NULL,
        my_score INTEGER NOT NULL,
        opp_score INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS app_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
]


class _Row:
    """A result row readable by column name or position, like sqlite3.Row."""

    __slots__ = ("_values", "_index")

    def __init__(self, values: tuple, index: dict[str, int]):
        self._values = values
        self._index = index

    def __getitem__(self, key):
        return self._values[key if isinstance(key, int) else self._index[key]]


class _Cursor:
    """Snapshot of one statement's results.

    libSQL reuses a single cursor per connection, so a later execute() would
    overwrite lastrowid/rows of an earlier one -- capture them immediately.
    """

    def __init__(self, cursor):
        self.lastrowid = cursor.lastrowid
        self.rowcount = cursor.rowcount
        index = {col[0]: i for i, col in enumerate(cursor.description or ())}
        self._rows = [_Row(row, index) for row in cursor.fetchall()] if index else []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _Connection:
    """libSQL's connection has no row_factory, so wrap it to return named rows."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql: str, params=()):
        return _Cursor(self._conn.execute(sql, tuple(params)))

    def executemany(self, sql: str, seq):
        return _Cursor(self._conn.executemany(sql, [tuple(p) for p in seq]))

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def get_connection() -> _Connection:
    turso_url = os.environ.get("TURSO_DATABASE_URL")
    if turso_url:
        return _Connection(libsql.connect(database=turso_url, auth_token=os.environ.get("TURSO_AUTH_TOKEN", "")))
    if os.environ.get("VERCEL"):
        # Vercel's filesystem is read-only and ephemeral -- a local file would fail or lose data.
        raise RuntimeError("TURSO_DATABASE_URL and TURSO_AUTH_TOKEN must be set when running on Vercel")
    return _Connection(libsql.connect(str(DB_PATH)))


def init_db(seed: bool = True):
    conn = get_connection()
    for statement in SCHEMA:
        conn.execute(statement)
    # Seed only once, ever -- so deleting the samples doesn't bring them back on restart.
    # The INSERT claims seeding atomically, so concurrent cold starts can't both seed.
    claimed = conn.execute("INSERT OR IGNORE INTO app_meta (key, value) VALUES ('seeded', '1')").rowcount == 1
    conn.commit()
    conn.close()
    if seed and claimed:
        _seed_sample_data()


def _seed_sample_data():
    samples = [
        ("2026-09-06", "Sports Hub Court 3", 120, "Saturday club night", [
            [(21, 18), (21, 15)],
            [(19, 21), (21, 17), (18, 21)],
            [(22, 20), (21, 19)],
        ]),
        ("2026-09-13", "Community Centre", 90, "", [
            [(21, 12), (21, 16)],
            [(15, 21), (17, 21)],
        ]),
        ("2026-10-04", "Sports Hub Court 3", 120, "Tough opponents", [
            [(21, 19), (19, 21), (21, 23)],
            [(30, 29), (21, 18)],
            [(14, 21), (16, 21)],
        ]),
    ]
    for session_date, venue, minutes, notes, matches in samples:
        session = create_session(session_date, venue, minutes, notes, _is_sample=True)
        for games in matches:
            log_match(session["id"], [{"my_score": m, "opp_score": o} for m, o in games])


# ---------- validation ----------

def _validate_date(value: str, field: str = "date") -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a valid date in YYYY-MM-DD format, got {value!r}")


def _validate_session_fields(session_date: str, venue: str, duration_minutes: int) -> tuple[str, str, int]:
    session_date = _validate_date(session_date, "session_date")
    venue = (venue or "").strip()
    if not venue:
        raise ValueError("venue is required")
    if isinstance(duration_minutes, bool) or not isinstance(duration_minutes, int):
        raise ValueError("duration_minutes must be a whole number")
    if not 0 < duration_minutes <= MAX_SESSION_MINUTES:
        raise ValueError(f"duration_minutes must be between 1 and {MAX_SESSION_MINUTES}")
    return session_date, venue, duration_minutes


def validate_game(my_score: int, opp_score: int, points_to_win: int = DEFAULT_POINTS_TO_WIN) -> bool:
    """Validate one game's final score under rally scoring. Returns True if I won."""
    for s in (my_score, opp_score):
        if isinstance(s, bool) or not isinstance(s, int) or s < 0:
            raise ValueError("scores must be non-negative whole numbers")
    cap = points_to_win + CAP_MARGIN
    winner, loser = max(my_score, opp_score), min(my_score, opp_score)
    score = f"{my_score}-{opp_score}"
    if winner == loser:
        raise ValueError(f"game {score} is a tie; a game must have a winner")
    if winner < points_to_win:
        raise ValueError(f"game {score} is unfinished; the winner needs at least {points_to_win} points")
    if winner == points_to_win:
        valid = loser <= points_to_win - 2
    elif winner < cap:
        valid = loser == winner - 2
    elif winner == cap:
        valid = loser >= cap - 2  # e.g. 30-28 (won by 2) or 30-29 (golden point)
    else:
        valid = False
    if not valid:
        raise ValueError(
            f"game {score} is not a valid final score for games to {points_to_win} "
            f"(win by 2, capped at {cap})"
        )
    return my_score > opp_score


def validate_match(games: list[dict], points_to_win: int = DEFAULT_POINTS_TO_WIN) -> bool:
    """Validate a best-of-3 match. Returns True if I won."""
    if points_to_win not in ALLOWED_POINT_TARGETS:
        raise ValueError(f"points_to_win must be one of {ALLOWED_POINT_TARGETS}")
    if not isinstance(games, list) or not 2 <= len(games) <= 3:
        raise ValueError("a match must have 2 or 3 games")
    my_wins = opp_wins = 0
    for i, game in enumerate(games, start=1):
        if my_wins == 2 or opp_wins == 2:
            raise ValueError(f"game {i} was played after the match was already decided")
        try:
            my_score, opp_score = game["my_score"], game["opp_score"]
        except (KeyError, TypeError):
            raise ValueError("each game needs my_score and opp_score")
        if validate_game(my_score, opp_score, points_to_win):
            my_wins += 1
        else:
            opp_wins += 1
    if max(my_wins, opp_wins) != 2:
        raise ValueError("match is incomplete; someone must win 2 games")
    return my_wins == 2


# ---------- sessions ----------

def create_session(session_date: str, venue: str, duration_minutes: int, notes: str = "", _is_sample: bool = False) -> dict:
    session_date, venue, duration_minutes = _validate_session_fields(session_date, venue, duration_minutes)
    conn = get_connection()
    cursor = conn.execute(
        "INSERT INTO sessions (session_date, venue, duration_minutes, notes, is_sample) VALUES (?, ?, ?, ?, ?)",
        (session_date, venue, duration_minutes, notes or "", int(_is_sample)),
    )
    conn.commit()
    conn.close()
    return get_session(cursor.lastrowid)


def update_session(session_id: int, session_date: str | None = None, venue: str | None = None,
                   duration_minutes: int | None = None, notes: str | None = None) -> dict:
    current = get_session(session_id)
    session_date, venue, duration_minutes = _validate_session_fields(
        session_date if session_date is not None else current["session_date"],
        venue if venue is not None else current["venue"],
        duration_minutes if duration_minutes is not None else current["duration_minutes"],
    )
    conn = get_connection()
    conn.execute(
        "UPDATE sessions SET session_date = ?, venue = ?, duration_minutes = ?, notes = ? WHERE id = ?",
        (session_date, venue, duration_minutes, notes if notes is not None else current["notes"], session_id),
    )
    conn.commit()
    conn.close()
    return get_session(session_id)


def _delete_sessions_where(conn, condition: str, params: tuple) -> int:
    """Delete sessions plus their matches and games (explicitly -- no reliance on FK cascades)."""
    session_ids = f"SELECT id FROM sessions WHERE {condition}"
    conn.execute(
        f"DELETE FROM games WHERE match_id IN (SELECT id FROM matches WHERE session_id IN ({session_ids}))", params
    )
    conn.execute(f"DELETE FROM matches WHERE session_id IN ({session_ids})", params)
    return conn.execute(f"DELETE FROM sessions WHERE {condition}", params).rowcount


def delete_session(session_id: int) -> dict:
    conn = get_connection()
    deleted = _delete_sessions_where(conn, "id = ?", (session_id,))
    conn.commit()
    conn.close()
    if not deleted:
        raise LookupError(f"session {session_id} not found")
    return {"deleted_session_id": session_id}


def get_session(session_id: int) -> dict:
    sessions = _load_sessions("WHERE s.id = ?", [session_id])
    if not sessions:
        raise LookupError(f"session {session_id} not found")
    return sessions[0]


def list_sessions(start_date: str | None = None, end_date: str | None = None, venue: str | None = None) -> list[dict]:
    clauses, params = [], []
    if start_date:
        clauses.append("s.session_date >= ?")
        params.append(_validate_date(start_date, "start_date"))
    if end_date:
        clauses.append("s.session_date <= ?")
        params.append(_validate_date(end_date, "end_date"))
    if venue:
        clauses.append("s.venue = ? COLLATE NOCASE")
        params.append(venue.strip())
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return _load_sessions(where, params)


def list_venues() -> list[str]:
    conn = get_connection()
    rows = conn.execute("SELECT DISTINCT venue FROM sessions ORDER BY venue COLLATE NOCASE").fetchall()
    conn.close()
    return [r["venue"] for r in rows]


def _load_sessions(where: str, params: list) -> list[dict]:
    conn = get_connection()
    session_rows = conn.execute(
        f"SELECT * FROM sessions s {where} ORDER BY s.session_date DESC, s.id DESC", params
    ).fetchall()
    sessions = [
        {
            "id": r["id"],
            "session_date": r["session_date"],
            "venue": r["venue"],
            "duration_minutes": r["duration_minutes"],
            "notes": r["notes"],
            "is_sample": bool(r["is_sample"]),
            "matches": [],
        }
        for r in session_rows
    ]
    by_id = {s["id"]: s for s in sessions}
    if by_id:
        placeholders = ",".join("?" * len(by_id))
        match_rows = conn.execute(
            f"SELECT * FROM matches WHERE session_id IN ({placeholders}) ORDER BY id", list(by_id)
        ).fetchall()
        matches = {r["id"]: _match_from_row(r) for r in match_rows}
        if matches:
            _attach_games(conn, matches)
        for m in matches.values():
            by_id[m["session_id"]]["matches"].append(m)
    conn.close()
    for s in sessions:
        s["wins"] = sum(m["won"] for m in s["matches"])
        s["losses"] = len(s["matches"]) - s["wins"]
    return sessions


# ---------- matches ----------

def _match_from_row(row) -> dict:
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "points_to_win": row["points_to_win"],
        "won": bool(row["won"]),
        "result": "win" if row["won"] else "loss",
        "notes": row["notes"],
        "games": [],
    }


def _attach_games(conn, matches: dict[int, dict]):
    placeholders = ",".join("?" * len(matches))
    for g in conn.execute(
        f"SELECT * FROM games WHERE match_id IN ({placeholders}) ORDER BY match_id, game_no", list(matches)
    ).fetchall():
        matches[g["match_id"]]["games"].append({"my_score": g["my_score"], "opp_score": g["opp_score"]})
    for m in matches.values():
        m["score"] = ", ".join(f"{g['my_score']}-{g['opp_score']}" for g in m["games"])


def _insert_games(conn, match_id: int, games: list[dict]):
    conn.executemany(
        "INSERT INTO games (match_id, game_no, my_score, opp_score) VALUES (?, ?, ?, ?)",
        [(match_id, i, g["my_score"], g["opp_score"]) for i, g in enumerate(games, start=1)],
    )


def get_match(match_id: int) -> dict:
    conn = get_connection()
    row = conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
    if row is None:
        conn.close()
        raise LookupError(f"match {match_id} not found")
    matches = {row["id"]: _match_from_row(row)}
    _attach_games(conn, matches)
    conn.close()
    return matches[match_id]


def log_match(session_id: int, games: list[dict], points_to_win: int = DEFAULT_POINTS_TO_WIN, notes: str = "") -> dict:
    won = validate_match(games, points_to_win)
    get_session(session_id)  # raises LookupError if missing
    conn = get_connection()
    cursor = conn.execute(
        "INSERT INTO matches (session_id, points_to_win, won, notes) VALUES (?, ?, ?, ?)",
        (session_id, points_to_win, int(won), notes or ""),
    )
    _insert_games(conn, cursor.lastrowid, games)
    conn.commit()
    conn.close()
    return get_match(cursor.lastrowid)


def update_match(match_id: int, games: list[dict] | None = None, points_to_win: int | None = None,
                 notes: str | None = None) -> dict:
    current = get_match(match_id)
    games = games if games is not None else current["games"]
    points_to_win = points_to_win if points_to_win is not None else current["points_to_win"]
    won = validate_match(games, points_to_win)
    conn = get_connection()
    conn.execute(
        "UPDATE matches SET points_to_win = ?, won = ?, notes = ? WHERE id = ?",
        (points_to_win, int(won), notes if notes is not None else current["notes"], match_id),
    )
    conn.execute("DELETE FROM games WHERE match_id = ?", (match_id,))
    _insert_games(conn, match_id, games)
    conn.commit()
    conn.close()
    return get_match(match_id)


def delete_match(match_id: int) -> dict:
    conn = get_connection()
    conn.execute("DELETE FROM games WHERE match_id = ?", (match_id,))
    deleted = conn.execute("DELETE FROM matches WHERE id = ?", (match_id,)).rowcount
    conn.commit()
    conn.close()
    if not deleted:
        raise LookupError(f"match {match_id} not found")
    return {"deleted_match_id": match_id}


# ---------- sample data ----------

def has_sample_data() -> bool:
    conn = get_connection()
    row = conn.execute("SELECT 1 FROM sessions WHERE is_sample = 1 LIMIT 1").fetchone()
    conn.close()
    return row is not None


def delete_sample_data() -> dict:
    conn = get_connection()
    deleted = _delete_sessions_where(conn, "is_sample = 1", ())
    conn.commit()
    conn.close()
    return {"deleted_sessions": deleted}


# ---------- stats ----------

def _win_pct(wins: int, matches: int) -> float | None:
    return round(100 * wins / matches, 1) if matches else None


def get_overall_stats(start_date: str | None = None, end_date: str | None = None) -> dict:
    clauses, params = [], []
    if start_date:
        clauses.append("s.session_date >= ?")
        params.append(_validate_date(start_date, "start_date"))
    if end_date:
        clauses.append("s.session_date <= ?")
        params.append(_validate_date(end_date, "end_date"))
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    conn = get_connection()
    s = conn.execute(
        f"SELECT COUNT(*) AS sessions, COALESCE(SUM(duration_minutes), 0) AS minutes FROM sessions s {where}",
        params,
    ).fetchone()
    m = conn.execute(
        f"SELECT COUNT(*) AS matches, COALESCE(SUM(m.won), 0) AS wins "
        f"FROM matches m JOIN sessions s ON s.id = m.session_id {where}",
        params,
    ).fetchone()
    g = conn.execute(
        f"SELECT COUNT(*) AS games, "
        f"COALESCE(SUM(g.my_score > g.opp_score), 0) AS games_won, "
        f"COALESCE(SUM(g.my_score), 0) AS points_won, COALESCE(SUM(g.opp_score), 0) AS points_lost "
        f"FROM games g JOIN matches m ON m.id = g.match_id JOIN sessions s ON s.id = m.session_id {where}",
        params,
    ).fetchone()
    conn.close()
    return {
        "start_date": start_date,
        "end_date": end_date,
        "sessions": s["sessions"],
        "hours_played": round(s["minutes"] / 60, 2),
        "matches": m["matches"],
        "wins": m["wins"],
        "losses": m["matches"] - m["wins"],
        "win_pct": _win_pct(m["wins"], m["matches"]),
        "games_won": g["games_won"],
        "games_lost": g["games"] - g["games_won"],
        "points_won": g["points_won"],
        "points_lost": g["points_lost"],
    }


def get_venue_stats() -> list[dict]:
    conn = get_connection()
    rows = conn.execute("""
        SELECT s.venue,
               COUNT(DISTINCT s.id) AS sessions,
               COUNT(m.id) AS matches,
               COALESCE(SUM(m.won), 0) AS wins
        FROM sessions s LEFT JOIN matches m ON m.session_id = s.id
        GROUP BY s.venue COLLATE NOCASE
        ORDER BY matches DESC, s.venue COLLATE NOCASE
    """).fetchall()
    conn.close()
    return [
        {
            "venue": r["venue"],
            "sessions": r["sessions"],
            "matches": r["matches"],
            "wins": r["wins"],
            "losses": r["matches"] - r["wins"],
            "win_pct": _win_pct(r["wins"], r["matches"]),
        }
        for r in rows
    ]


def get_monthly_trend(months: int = 12) -> list[dict]:
    """Per-month totals for the most recent `months` months that have sessions, oldest first."""
    if isinstance(months, bool) or not isinstance(months, int) or months < 1:
        raise ValueError("months must be a positive whole number")
    conn = get_connection()
    rows = conn.execute("""
        SELECT month, sessions, minutes, matches, wins FROM (
            SELECT substr(s.session_date, 1, 7) AS month,
                   COUNT(*) AS sessions,
                   SUM(s.duration_minutes) AS minutes,
                   SUM(mc.matches) AS matches,
                   SUM(mc.wins) AS wins
            FROM sessions s
            JOIN (
                SELECT s2.id AS session_id, COUNT(m.id) AS matches, COALESCE(SUM(m.won), 0) AS wins
                FROM sessions s2 LEFT JOIN matches m ON m.session_id = s2.id
                GROUP BY s2.id
            ) mc ON mc.session_id = s.id
            GROUP BY month
            ORDER BY month DESC
            LIMIT ?
        ) ORDER BY month
    """, (months,)).fetchall()
    conn.close()
    return [
        {
            "month": r["month"],
            "sessions": r["sessions"],
            "hours_played": round(r["minutes"] / 60, 2),
            "matches": r["matches"],
            "wins": r["wins"],
            "losses": r["matches"] - r["wins"],
            "win_pct": _win_pct(r["wins"], r["matches"]),
        }
        for r in rows
    ]
