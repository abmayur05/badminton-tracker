import os
import tempfile
from pathlib import Path

import pytest

# Point the app at a throwaway database BEFORE main/database are imported,
# so importing main (which calls init_db) never touches the real badminton.db.
os.environ["BADMINTON_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "import.db")

import database as db  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db(seed=False)
    yield


@pytest.fixture
def session():
    return db.create_session("2026-10-01", "Sports Hub", 90)


def games(*scores):
    return [{"my_score": m, "opp_score": o} for m, o in scores]
