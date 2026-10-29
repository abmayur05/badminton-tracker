"""
Badminton Tracker's persistence layer -- SQLite (via libSQL), shared by the
website and the MCP server. Tracks personal badminton play: sessions (date,
venue, duration in hours) and the matches played in each session -- one
score per match, any number of matches per session.

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
MAX_SESSION_HOURS = 24

# Durations are entered in whole hours but stored in minutes, so sessions
# logged before the switch to hours keep their exact length.
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
        notes TEXT NOT NULL DEFAULT '',
        my_score INTEGER,
        opp_score INTEGER
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
    # Turso enforces foreign keys; do the same locally so tests catch FK violations.
    conn = _Connection(libsql.connect(str(DB_PATH)))
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(seed: bool = True):
    conn = get_connection()
    for statement in SCHEMA:
        conn.execute(statement)
    _migrate_to_single_score_matches(conn)
    # Seed only once, ever -- so deleting the samples doesn't bring them back on restart.
    # The INSERT claims seeding atomically, so concurrent cold starts can't both seed.
    claimed = conn.execute("INSERT OR IGNORE INTO app_meta (key, value) VALUES ('seeded', '1')").rowcount == 1
    conn.commit()
    conn.close()
    if seed and claimed:
        _seed_sample_data()


def _migrate_to_single_score_matches(conn):
    """One-time upgrade: matches used to be best-of-3 with scores in a `games` table.
    Each old game becomes its own match (same session, target, and order).

    Driven by the schema itself (not a flag), so it is safe to re-run after a failure."""
    matches_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'matches'").fetchone()[0]
    for column in ("my_score", "opp_score"):
        if column not in matches_sql:
            conn.execute(f"ALTER TABLE matches ADD COLUMN {column} INTEGER")
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'games'").fetchone():
        return
    old_games = conn.execute("""
        SELECT m.id AS match_id, m.session_id, m.points_to_win, m.notes, g.my_score, g.opp_score
        FROM matches m JOIN games g ON g.match_id = m.id
        ORDER BY m.id, g.game_no
    """).fetchall()
    # Drop games first: its rows reference matches, and Turso enforces foreign keys.
    conn.execute("DROP TABLE games")
    conn.execute("DELETE FROM matches")
    previous_match = None
    for g in old_games:
        conn.execute(
            "INSERT INTO matches (session_id, points_to_win, won, notes, my_score, opp_score) VALUES (?, ?, ?, ?, ?, ?)",
            (g["session_id"], g["points_to_win"], int(g["my_score"] > g["opp_score"]),
             g["notes"] if g["match_id"] != previous_match else "", g["my_score"], g["opp_score"]),
        )
        previous_match = g["match_id"]


def _seed_sample_data():
    samples = [
        ("2026-09-06", "Sports Hub Court 3", 2, "Saturday club night",
         [(21, 18), (21, 15), (19, 21), (21, 17), (22, 20)]),
        ("2026-09-13", "Community Centre", 1, "",
         [(21, 12), (15, 21), (17, 21)]),
        ("2026-10-04", "Sports Hub Court 3", 2, "Tough opponents",
         [(21, 19), (21, 23), (30, 29), (14, 21)]),
    ]
    for session_date, venue, hours, notes, scores in samples:
        session = create_session(session_date, venue, hours, notes, _is_sample=True)
        for my_score, opp_score in scores:
            log_match(session["id"], my_score, opp_score)


# ---------- validation ----------

def _validate_date(value: str, field: str = "date") -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a valid date in YYYY-MM-DD format, got {value!r}")


def _validate_hours(duration_hours) -> int:
    if isinstance(duration_hours, float) and duration_hours.is_integer():
        duration_hours = int(duration_hours)
    if isinstance(duration_hours, bool) or not isinstance(duration_hours, int):
        raise ValueError("duration_hours must be a whole number of hours, e.g. 1, 2, 3")
    if not 1 <= duration_hours <= MAX_SESSION_HOURS:
        raise ValueError(f"duration_hours must be between 1 and {MAX_SESSION_HOURS}")
    return duration_hours


def _validate_session_fields(session_date: str, venue: str) -> tuple[str, str]:
    session_date = _validate_date(session_date, "session_date")
    venue = (venue or "").strip()
    if not venue:
        raise ValueError("venue is required")
    return session_date, venue


def validate_score(my_score: int, opp_score: int, points_to_win: int = DEFAULT_POINTS_TO_WIN) -> bool:
    """Validate a match's final score under rally scoring. Returns True if I won."""
    if points_to_win not in ALLOWED_POINT_TARGETS:
        raise ValueError(f"points_to_win must be one of {ALLOWED_POINT_TARGETS}")
    for s in (my_score, opp_score):
        if isinstance(s, bool) or not isinstance(s, int) or s < 0:
            raise ValueError("scores must be non-negative whole numbers")
    cap = points_to_win + CAP_MARGIN
    winner, loser = max(my_score, opp_score), min(my_score, opp_score)
    score = f"{my_score}-{opp_score}"
    if winner == loser:
        raise ValueError(f"score {score} is a tie; a match must have a winner")
    if winner < points_to_win:
        raise ValueError(f"score {score} is unfinished; the winner needs at least {points_to_win} points")
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
            f"score {score} is not a valid final score for games to {points_to_win} "
            f"(win by 2, capped at {cap})"
        )
    return my_score > opp_score


# ---------- sessions ----------

def create_session(session_date: str, venue: str, duration_hours: int, notes: str = "", _is_sample: bool = False) -> dict:
    session_date, venue = _validate_session_fields(session_date, venue)
    minutes = _validate_hours(duration_hours) * 60
    conn = get_connection()
    session_id = conn.execute(
        "INSERT INTO sessions (session_date, venue, duration_minutes, notes, is_sample) VALUES (?, ?, ?, ?, ?)",
        (session_date, venue, minutes, notes or "", int(_is_sample)),
    ).lastrowid
    conn.commit()
    conn.close()
    return get_session(session_id)


def update_session(session_id: int, session_date: str | None = None, venue: str | None = None,
                   duration_hours: int | None = None, notes: str | None = None) -> dict:
    current = get_session(session_id)
    session_date, venue = _validate_session_fields(
        session_date if session_date is not None else current["session_date"],
        venue if venue is not None else current["venue"],
    )
    conn = get_connection()
    conn.execute(
        "UPDATE sessions SET session_date = ?, venue = ?, notes = ? WHERE id = ?",
        (session_date, venue, notes if notes is not None else current["notes"], session_id),
    )
    if duration_hours is not None:
        conn.execute("UPDATE sessions SET duration_minutes = ? WHERE id = ?",
                     (_validate_hours(duration_hours) * 60, session_id))
    conn.commit()
    conn.close()
    return get_session(session_id)


def _delete_sessions_where(conn, condition: str, params: tuple) -> int:
    """Delete sessions plus their matches (explicitly -- no reliance on FK cascades)."""
    conn.execute(f"DELETE FROM matches WHERE session_id IN (SELECT id FROM sessions WHERE {condition})", params)
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


def _hours(minutes: int) -> int | float:
    hours = round(minutes / 60, 2)
    return int(hours) if hours == int(hours) else hours


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
            "duration_hours": _hours(r["duration_minutes"]),
            "notes": r["notes"],
            "is_sample": bool(r["is_sample"]),
            "matches": [],
        }
        for r in session_rows
    ]
    by_id = {s["id"]: s for s in sessions}
    if by_id:
        placeholders = ",".join("?" * len(by_id))
        for r in conn.execute(
            f"SELECT * FROM matches WHERE session_id IN ({placeholders}) ORDER BY id", list(by_id)
        ).fetchall():
            by_id[r["session_id"]]["matches"].append(_match_from_row(r))
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
        "my_score": row["my_score"],
        "opp_score": row["opp_score"],
        "score": f"{row['my_score']}-{row['opp_score']}",
        "points_to_win": row["points_to_win"],
        "won": bool(row["won"]),
        "result": "win" if row["won"] else "loss",
        "notes": row["notes"],
    }


def get_match(match_id: int) -> dict:
    conn = get_connection()
    row = conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
    conn.close()
    if row is None:
        raise LookupError(f"match {match_id} not found")
    return _match_from_row(row)


def log_match(session_id: int, my_score: int, opp_score: int,
              points_to_win: int = DEFAULT_POINTS_TO_WIN, notes: str = "") -> dict:
    won = validate_score(my_score, opp_score, points_to_win)
    get_session(session_id)  # raises LookupError if missing
    conn = get_connection()
    match_id = conn.execute(
        "INSERT INTO matches (session_id, points_to_win, won, notes, my_score, opp_score) VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, points_to_win, int(won), notes or "", my_score, opp_score),
    ).lastrowid
    conn.commit()
    conn.close()
    return get_match(match_id)


def update_match(match_id: int, my_score: int | None = None, opp_score: int | None = None,
                 points_to_win: int | None = None, notes: str | None = None) -> dict:
    current = get_match(match_id)
    my_score = my_score if my_score is not None else current["my_score"]
    opp_score = opp_score if opp_score is not None else current["opp_score"]
    points_to_win = points_to_win if points_to_win is not None else current["points_to_win"]
    won = validate_score(my_score, opp_score, points_to_win)
    conn = get_connection()
    conn.execute(
        "UPDATE matches SET my_score = ?, opp_score = ?, points_to_win = ?, won = ?, notes = ? WHERE id = ?",
        (my_score, opp_score, points_to_win, int(won), notes if notes is not None else current["notes"], match_id),
    )
    conn.commit()
    conn.close()
    return get_match(match_id)


def delete_match(match_id: int) -> dict:
    conn = get_connection()
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
        f"SELECT COUNT(*) AS matches, COALESCE(SUM(m.won), 0) AS wins, "
        f"COALESCE(SUM(m.my_score), 0) AS points_won, COALESCE(SUM(m.opp_score), 0) AS points_lost "
        f"FROM matches m JOIN sessions s ON s.id = m.session_id {where}",
        params,
    ).fetchone()
    conn.close()
    return {
        "start_date": start_date,
        "end_date": end_date,
        "sessions": s["sessions"],
        "hours_played": _hours(s["minutes"]),
        "matches": m["matches"],
        "wins": m["wins"],
        "losses": m["matches"] - m["wins"],
        "win_pct": _win_pct(m["wins"], m["matches"]),
        "points_won": m["points_won"],
        "points_lost": m["points_lost"],
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
            "hours_played": _hours(r["minutes"]),
            "matches": r["matches"],
            "wins": r["wins"],
            "losses": r["matches"] - r["wins"],
            "win_pct": _win_pct(r["wins"], r["matches"]),
        }
        for r in rows
    ]
