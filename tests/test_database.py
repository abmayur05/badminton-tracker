import pytest

import database as db


# ---------- score validation ----------

@pytest.mark.parametrize("my, opp, target, won", [
    (21, 0, 21, True),
    (21, 19, 21, True),
    (19, 21, 21, False),
    (22, 20, 21, True),
    (27, 29, 21, False),
    (30, 28, 21, True),
    (30, 29, 21, True),
    (29, 30, 21, False),
    (15, 13, 15, True),
    (24, 23, 15, True),
    (11, 5, 11, True),
    (20, 19, 11, True),
])
def test_valid_scores(my, opp, target, won):
    assert db.validate_score(my, opp, target) is won


@pytest.mark.parametrize("my, opp, target", [
    (21, 20, 21),   # not won by 2
    (20, 18, 21),   # unfinished
    (25, 15, 21),   # would have ended at 21
    (23, 20, 21),   # past 21 must be exactly +2
    (31, 29, 21),   # over the cap
    (30, 27, 21),   # 30 only reachable from 28-28 or 29-29
    (21, 21, 21),   # tie
    (-1, 21, 21),   # negative
    (21, 13, 15),   # wrong target
    (25, 10, 25),   # target not allowed
])
def test_invalid_scores(my, opp, target):
    with pytest.raises(ValueError):
        db.validate_score(my, opp, target)


# ---------- sessions ----------

@pytest.mark.parametrize("kwargs", [
    {"session_date": "2026-13-01"},
    {"session_date": "01/10/2026"},
    {"venue": "   "},
    {"duration_hours": 0},
    {"duration_hours": 25},
    {"duration_hours": 1.5},
    {"duration_hours": "2"},
])
def test_session_validation(kwargs):
    args = {"session_date": "2026-10-01", "venue": "Hall", "duration_hours": 1, **kwargs}
    with pytest.raises(ValueError):
        db.create_session(**args)


def test_duration_is_whole_hours(session):
    assert session["duration_hours"] == 2
    assert db.create_session("2026-10-02", "Hall", 3.0)["duration_hours"] == 3


def test_update_session_changes_only_given_fields(session):
    updated = db.update_session(session["id"], venue="Community Centre")
    assert updated["venue"] == "Community Centre"
    assert updated["session_date"] == "2026-10-01"
    assert updated["duration_hours"] == 2
    assert db.update_session(session["id"], duration_hours=3)["duration_hours"] == 3


def test_unknown_ids_raise_lookup_error():
    for call in (lambda: db.get_session(999), lambda: db.delete_session(999),
                 lambda: db.update_match(999, notes="x"), lambda: db.delete_match(999),
                 lambda: db.log_match(999, 21, 1)):
        with pytest.raises(LookupError):
            call()


def test_delete_session_removes_its_matches(session):
    match = db.log_match(session["id"], 21, 18)
    db.delete_session(session["id"])
    with pytest.raises(LookupError):
        db.get_match(match["id"])


def test_list_sessions_filters(session):
    db.create_session("2026-11-05", "Community Centre", 1)
    assert [s["venue"] for s in db.list_sessions(start_date="2026-11-01")] == ["Community Centre"]
    assert [s["session_date"] for s in db.list_sessions(venue="sports hub")] == ["2026-10-01"]


# ---------- matches ----------

def test_any_number_of_matches_per_session(session):
    for i in range(12):
        db.log_match(session["id"], 21, i)
    s = db.get_session(session["id"])
    assert len(s["matches"]) == 12
    assert (s["wins"], s["losses"]) == (12, 0)


def test_log_and_update_match_recalculates_result(session):
    match = db.log_match(session["id"], 21, 18, notes="good")
    assert (match["score"], match["result"], match["my_score"], match["opp_score"]) == ("21-18", "win", 21, 18)

    updated = db.update_match(match["id"], my_score=19, opp_score=21)
    assert (updated["score"], updated["result"], updated["notes"]) == ("19-21", "loss", "good")

    updated = db.update_match(match["id"], points_to_win=11, my_score=11, opp_score=4)
    assert (updated["score"], updated["points_to_win"], updated["won"]) == ("11-4", 11, True)


def test_invalid_update_leaves_match_unchanged(session):
    match = db.log_match(session["id"], 21, 18)
    with pytest.raises(ValueError):
        db.update_match(match["id"], opp_score=20)
    assert db.get_match(match["id"])["score"] == "21-18"


# ---------- stats ----------

def test_overall_stats(session):
    db.log_match(session["id"], 21, 18)
    db.log_match(session["id"], 19, 21)
    assert db.get_overall_stats() == {
        "start_date": None, "end_date": None,
        "sessions": 1, "hours_played": 2,
        "matches": 2, "wins": 1, "losses": 1, "win_pct": 50.0,
        "points_won": 40, "points_lost": 39,
    }


def test_overall_stats_empty_and_date_filtered(session):
    db.log_match(session["id"], 21, 18)
    empty = db.get_overall_stats(start_date="2027-01-01")
    assert empty["matches"] == 0 and empty["win_pct"] is None


def test_venue_stats_groups_case_insensitively(session):
    db.log_match(session["id"], 21, 18)
    other = db.create_session("2026-10-02", "sports hub", 1)
    db.log_match(other["id"], 10, 21)
    db.create_session("2026-10-03", "Park", 1)  # no matches
    by_venue = {v["venue"].lower(): v for v in db.get_venue_stats()}
    assert by_venue["sports hub"]["sessions"] == 2
    assert by_venue["sports hub"]["win_pct"] == 50.0
    assert by_venue["park"]["matches"] == 0 and by_venue["park"]["win_pct"] is None


def test_monthly_trend(session):
    db.log_match(session["id"], 21, 18)
    db.create_session("2026-10-20", "Park", 1)
    nov = db.create_session("2026-11-02", "Park", 1)
    db.log_match(nov["id"], 10, 21)
    trend = db.get_monthly_trend()
    assert [t["month"] for t in trend] == ["2026-10", "2026-11"]
    assert trend[0]["sessions"] == 2 and trend[0]["hours_played"] == 3 and trend[0]["wins"] == 1
    assert trend[1]["win_pct"] == 0.0
    assert [t["month"] for t in db.get_monthly_trend(1)] == ["2026-11"]


# ---------- sample data ----------

def test_sample_data_seeds_once_and_deletes_only_samples(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "seeded.db")
    db.init_db()
    assert db.has_sample_data()
    sample_sessions = len(db.list_sessions())
    assert sample_sessions > 0

    mine = db.create_session("2026-10-28", "My Court", 1)
    db.log_match(mine["id"], 21, 15)
    assert db.delete_sample_data() == {"deleted_sessions": sample_sessions}
    assert [s["id"] for s in db.list_sessions()] == [mine["id"]]
    assert not db.has_sample_data()
    conn = db.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 1
    conn.close()

    db.init_db()  # simulate restart: samples must not come back
    assert not db.has_sample_data()


# ---------- migration from best-of-3 matches ----------

def test_migrates_old_multi_game_matches_into_single_score_matches(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "old.db")
    conn = db.get_connection()
    for sql in [
        "CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, session_date TEXT NOT NULL, venue TEXT NOT NULL, "
        "duration_minutes INTEGER NOT NULL, notes TEXT NOT NULL DEFAULT '', is_sample INTEGER NOT NULL DEFAULT 0)",
        "CREATE TABLE matches (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL REFERENCES sessions(id), "
        "points_to_win INTEGER NOT NULL DEFAULT 21, won INTEGER NOT NULL, notes TEXT NOT NULL DEFAULT '')",
        "CREATE TABLE games (id INTEGER PRIMARY KEY AUTOINCREMENT, match_id INTEGER NOT NULL REFERENCES matches(id), "
        "game_no INTEGER NOT NULL, my_score INTEGER NOT NULL, opp_score INTEGER NOT NULL)",
        "CREATE TABLE app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        "INSERT INTO app_meta VALUES ('seeded', '1')",
        "INSERT INTO sessions (session_date, venue, duration_minutes) VALUES ('2026-10-29', 'Dazhong', 90)",
        "INSERT INTO matches (session_id, won, notes) VALUES (1, 0, 'first')",
        "INSERT INTO matches (session_id, won, notes) VALUES (1, 1, 'second')",
        "INSERT INTO games (match_id, game_no, my_score, opp_score) VALUES (1, 1, 21, 19), (1, 2, 19, 21), (1, 3, 21, 23)",
        "INSERT INTO games (match_id, game_no, my_score, opp_score) VALUES (2, 1, 30, 29), (2, 2, 21, 18)",
    ]:
        conn.execute(sql)
    conn.commit()
    conn.close()

    db.init_db()
    db.init_db()  # second run must not migrate again

    s = db.get_session(1)
    assert s["duration_hours"] == 1.5  # legacy minutes kept exactly
    assert [m["score"] for m in s["matches"]] == ["21-19", "19-21", "21-23", "30-29", "21-18"]
    assert [m["result"] for m in s["matches"]] == ["win", "loss", "loss", "win", "win"]
    assert [m["notes"] for m in s["matches"]] == ["first", "", "", "second", ""]
    assert not db.has_sample_data()
    conn = db.get_connection()
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'games'").fetchone() is None
    conn.close()


# ---------- connection selection ----------

def test_uses_turso_when_url_is_set(monkeypatch):
    calls = []
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://example.turso.io")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "secret")
    monkeypatch.setattr(db.libsql, "connect", lambda **kwargs: calls.append(kwargs) or object())
    db.get_connection()
    assert calls == [{"database": "libsql://example.turso.io", "auth_token": "secret"}]


def test_refuses_local_file_on_vercel(monkeypatch):
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    monkeypatch.setenv("VERCEL", "1")
    with pytest.raises(RuntimeError, match="TURSO_DATABASE_URL"):
        db.get_connection()
