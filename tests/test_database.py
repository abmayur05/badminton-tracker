import pytest

import database as db
from conftest import games


# ---------- game score validation ----------

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
def test_valid_game_scores(my, opp, target, won):
    assert db.validate_game(my, opp, target) is won


@pytest.mark.parametrize("my, opp, target", [
    (21, 20, 21),   # not won by 2
    (20, 18, 21),   # unfinished
    (25, 15, 21),   # game would have ended at 21
    (23, 20, 21),   # past 21 must be exactly +2
    (31, 29, 21),   # over the cap
    (30, 27, 21),   # 30 only reachable from 28-28 or 29-29
    (21, 21, 21),   # tie
    (-1, 21, 21),   # negative
    (21, 13, 15),   # wrong target
])
def test_invalid_game_scores(my, opp, target):
    with pytest.raises(ValueError):
        db.validate_game(my, opp, target)


# ---------- match validation ----------

def test_match_result_is_derived_from_scores():
    assert db.validate_match(games((21, 18), (21, 15))) is True
    assert db.validate_match(games((21, 18), (15, 21), (19, 21))) is False


@pytest.mark.parametrize("bad", [
    games((21, 18)),                               # one game
    games((21, 18), (15, 21)),                     # 1-1, incomplete
    games((21, 18), (21, 15), (21, 10)),           # third game after already decided
    games((21, 18), (21, 15), (21, 10), (21, 9)),  # four games
    [{"my_score": 21}, {"my_score": 21, "opp_score": 3}],  # missing score
])
def test_invalid_matches(bad):
    with pytest.raises(ValueError):
        db.validate_match(bad)


def test_points_to_win_must_be_allowed():
    with pytest.raises(ValueError):
        db.validate_match(games((25, 10), (25, 10)), points_to_win=25)


# ---------- sessions ----------

@pytest.mark.parametrize("kwargs", [
    {"session_date": "2026-13-01"},
    {"session_date": "01/10/2026"},
    {"venue": "   "},
    {"duration_minutes": 0},
    {"duration_minutes": 2000},
])
def test_session_validation(kwargs):
    args = {"session_date": "2026-10-01", "venue": "Hall", "duration_minutes": 60, **kwargs}
    with pytest.raises(ValueError):
        db.create_session(**args)


def test_update_session_changes_only_given_fields(session):
    updated = db.update_session(session["id"], venue="Community Centre")
    assert updated["venue"] == "Community Centre"
    assert updated["session_date"] == "2026-10-01"
    assert updated["duration_minutes"] == 90


def test_unknown_ids_raise_lookup_error():
    for call in (lambda: db.get_session(999), lambda: db.delete_session(999),
                 lambda: db.update_match(999, notes="x"), lambda: db.delete_match(999),
                 lambda: db.log_match(999, games((21, 1), (21, 1)))):
        with pytest.raises(LookupError):
            call()


def test_delete_session_cascades_to_matches_and_games(session):
    match = db.log_match(session["id"], games((21, 18), (21, 15)))
    db.delete_session(session["id"])
    with pytest.raises(LookupError):
        db.get_match(match["id"])
    conn = db.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 0
    conn.close()


def test_list_sessions_filters(session):
    db.create_session("2026-11-05", "Community Centre", 60)
    assert [s["venue"] for s in db.list_sessions(start_date="2026-11-01")] == ["Community Centre"]
    assert [s["session_date"] for s in db.list_sessions(venue="sports hub")] == ["2026-10-01"]


# ---------- matches ----------

def test_log_and_update_match_recalculates_result(session):
    match = db.log_match(session["id"], games((21, 18), (21, 15)), notes="good")
    assert match["result"] == "win"
    assert match["score"] == "21-18, 21-15"

    updated = db.update_match(match["id"], games=games((18, 21), (21, 15), (19, 21)))
    assert updated["result"] == "loss"
    assert len(updated["games"]) == 3
    assert updated["notes"] == "good"

    session_after = db.get_session(session["id"])
    assert (session_after["wins"], session_after["losses"]) == (0, 1)


def test_invalid_update_leaves_match_unchanged(session):
    match = db.log_match(session["id"], games((21, 18), (21, 15)))
    with pytest.raises(ValueError):
        db.update_match(match["id"], games=games((21, 20), (21, 15)))
    assert db.get_match(match["id"])["score"] == "21-18, 21-15"


# ---------- stats ----------

def test_overall_stats(session):
    db.log_match(session["id"], games((21, 18), (21, 15)))
    db.log_match(session["id"], games((19, 21), (21, 17), (18, 21)))
    stats = db.get_overall_stats()
    assert stats == {
        "start_date": None, "end_date": None,
        "sessions": 1, "hours_played": 1.5,
        "matches": 2, "wins": 1, "losses": 1, "win_pct": 50.0,
        "games_won": 3, "games_lost": 2,
        "points_won": 21 + 21 + 19 + 21 + 18, "points_lost": 18 + 15 + 21 + 17 + 21,
    }


def test_overall_stats_empty_and_date_filtered(session):
    db.log_match(session["id"], games((21, 18), (21, 15)))
    empty = db.get_overall_stats(start_date="2027-01-01")
    assert empty["matches"] == 0 and empty["win_pct"] is None


def test_venue_stats_groups_case_insensitively(session):
    db.log_match(session["id"], games((21, 18), (21, 15)))
    other = db.create_session("2026-10-02", "sports hub", 60)
    db.log_match(other["id"], games((10, 21), (10, 21)))
    db.create_session("2026-10-03", "Park", 30)  # no matches
    by_venue = {v["venue"].lower(): v for v in db.get_venue_stats()}
    assert by_venue["sports hub"]["sessions"] == 2
    assert by_venue["sports hub"]["win_pct"] == 50.0
    assert by_venue["park"]["matches"] == 0 and by_venue["park"]["win_pct"] is None


def test_monthly_trend(session):
    db.log_match(session["id"], games((21, 18), (21, 15)))
    db.create_session("2026-10-20", "Park", 30)
    nov = db.create_session("2026-11-02", "Park", 60)
    db.log_match(nov["id"], games((10, 21), (10, 21)))
    trend = db.get_monthly_trend()
    assert [t["month"] for t in trend] == ["2026-10", "2026-11"]
    assert trend[0]["sessions"] == 2 and trend[0]["hours_played"] == 2.0 and trend[0]["wins"] == 1
    assert trend[1]["win_pct"] == 0.0
    assert [t["month"] for t in db.get_monthly_trend(1)] == ["2026-11"]


# ---------- sample data ----------

def test_sample_data_seeds_once_and_deletes_only_samples(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "seeded.db")
    db.init_db()
    assert db.has_sample_data()
    sample_sessions = len(db.list_sessions())
    assert sample_sessions > 0

    mine = db.create_session("2026-10-28", "My Court", 60)
    assert db.delete_sample_data() == {"deleted_sessions": sample_sessions}
    assert [s["id"] for s in db.list_sessions()] == [mine["id"]]
    assert not db.has_sample_data()

    db.init_db()  # simulate restart: samples must not come back
    assert not db.has_sample_data()
