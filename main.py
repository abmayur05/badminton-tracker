"""
Badminton Tracker -- one running application, two front doors onto the same
SQLite database of personal badminton sessions and matches:

  1. A website (served from ./static) -- for you, in a browser
  2. An MCP server, mounted at /mcp -- for AI assistants, over HTTP

Run:
    uv run python -m uvicorn main:app --reload

Website: http://127.0.0.1:8000    MCP (Streamable HTTP): http://127.0.0.1:8000/mcp
"""
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastmcp import FastMCP
from pydantic import BaseModel

import database as db

STATIC_DIR = Path(__file__).parent / "static"

db.init_db()


# ---------- Step 1: MCP server ----------
mcp = FastMCP("Badminton Tracker")


@mcp.tool
def log_session(session_date: str, venue: str, duration_hours: int, notes: str = "") -> dict:
    """Log a badminton play session. session_date must be YYYY-MM-DD; duration_hours is a
    whole number of hours (1, 2, 3...). Returns the session with its id."""
    return db.create_session(session_date, venue, duration_hours, notes)


@mcp.tool
def log_match(session_id: int, my_score: int, opp_score: int, points_to_win: int = 21, notes: str = "") -> dict:
    """Log one match (a single game) in a session, scored from my side's perspective,
    e.g. my_score=21, opp_score=18. A session can have any number of matches.
    points_to_win is 21 (default), 15, or 11; won by 2, capped at points_to_win + 9 (21 -> 30).
    The win/loss result is calculated from the score."""
    return db.log_match(session_id, my_score, opp_score, points_to_win, notes)


@mcp.tool
def list_sessions(start_date: str = "", end_date: str = "", venue: str = "") -> list[dict]:
    """List sessions (newest first) with their matches, optionally filtered by date range (YYYY-MM-DD) and venue."""
    return db.list_sessions(start_date or None, end_date or None, venue or None)


@mcp.tool
def get_session(session_id: int) -> dict:
    """Get one session with all its matches and scores."""
    return db.get_session(session_id)


@mcp.tool
def update_session(session_id: int, session_date: str | None = None, venue: str | None = None,
                   duration_hours: int | None = None, notes: str | None = None) -> dict:
    """Edit a session. Only the fields you pass are changed."""
    return db.update_session(session_id, session_date, venue, duration_hours, notes)


@mcp.tool
def delete_session(session_id: int) -> dict:
    """Delete a session and all of its matches. This cannot be undone."""
    return db.delete_session(session_id)


@mcp.tool
def update_match(match_id: int, my_score: int | None = None, opp_score: int | None = None,
                 points_to_win: int | None = None, notes: str | None = None) -> dict:
    """Edit a match. Only the fields you pass are changed; the result is recalculated."""
    return db.update_match(match_id, my_score, opp_score, points_to_win, notes)


@mcp.tool
def delete_match(match_id: int) -> dict:
    """Delete one match. This cannot be undone."""
    return db.delete_match(match_id)


@mcp.tool
def get_overall_stats(start_date: str = "", end_date: str = "") -> dict:
    """Overall record: sessions, hours, matches, wins/losses, win %, and points won/lost.
    Optionally limited to a date range (YYYY-MM-DD)."""
    return db.get_overall_stats(start_date or None, end_date or None)


@mcp.tool
def get_venue_stats() -> list[dict]:
    """Sessions, matches, and win % at each venue."""
    return db.get_venue_stats()


@mcp.tool
def get_monthly_trend(months: int = 12) -> list[dict]:
    """Month-by-month sessions, hours, matches, and win % for the most recent months played, oldest first."""
    return db.get_monthly_trend(months)


@mcp.tool
def delete_sample_data() -> dict:
    """Delete the sample sessions that were seeded on first run. Your own data is untouched."""
    return db.delete_sample_data()


@mcp.resource("badminton://venues")
def known_venues() -> list[str]:
    """Venues already used, so new sessions can reuse consistent names."""
    return db.list_venues()


@mcp.prompt
def weekly_recap(week_start: str) -> str:
    """Guides the AI to build a weekly badminton recap from this server's own tools."""
    return f"""Build my badminton recap for the 7 days starting {week_start} (YYYY-MM-DD).

1. Work out week_end = {week_start} + 6 days.
2. Call list_sessions with start_date='{week_start}' and end_date=week_end.
3. Call get_overall_stats with the same dates.
4. Present it as:
   Week of {week_start}
   Sessions: N ({{hours}}h)  |  Matches: W-L ({{win %}})
   Per session: date, venue, each match's score and result
   Points won/lost
   One or two observations (e.g. close scores, best venue this week).

If there are no sessions that week, say so plainly instead of inventing data.
"""


# path="/" here, NOT "/mcp" -- app.mount() below adds that prefix.
# stateless_http: no per-client session kept in memory, so it works on serverless
# hosts (Vercel) where consecutive requests can land on different instances.
mcp_app = mcp.http_app(path="/", stateless_http=True)


# ---------- Step 2: FastAPI app, MCP lifespan wired in at construction ----------
app = FastAPI(title="Badminton Tracker", lifespan=mcp_app.lifespan)


class McpPathAlias:
    """Serve /mcp directly instead of redirecting to /mcp/ -- some MCP clients
    (e.g. Claude's custom connectors) don't follow redirects on POST."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/mcp":
            scope = {**scope, "path": "/mcp/", "raw_path": b"/mcp/"}
        await self.app(scope, receive, send)


app.add_middleware(McpPathAlias)


@app.exception_handler(ValueError)
async def handle_value_error(request: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(LookupError)
async def handle_lookup_error(request: Request, exc: LookupError):
    return JSONResponse(status_code=404, content={"detail": str(exc).strip("'\"")})


class NewSession(BaseModel):
    session_date: str
    venue: str
    duration_hours: int
    notes: str = ""


class SessionUpdate(BaseModel):
    session_date: str | None = None
    venue: str | None = None
    duration_hours: int | None = None
    notes: str | None = None


class NewMatch(BaseModel):
    my_score: int
    opp_score: int
    points_to_win: int = db.DEFAULT_POINTS_TO_WIN
    notes: str = ""


class MatchUpdate(BaseModel):
    my_score: int | None = None
    opp_score: int | None = None
    points_to_win: int | None = None
    notes: str | None = None


@app.get("/api/sessions")
def api_list_sessions(start_date: str | None = None, end_date: str | None = None, venue: str | None = None):
    return db.list_sessions(start_date, end_date, venue)


@app.post("/api/sessions", status_code=201)
def api_create_session(body: NewSession):
    return db.create_session(body.session_date, body.venue, body.duration_hours, body.notes)


@app.get("/api/sessions/{session_id}")
def api_get_session(session_id: int):
    return db.get_session(session_id)


@app.put("/api/sessions/{session_id}")
def api_update_session(session_id: int, body: SessionUpdate):
    return db.update_session(session_id, body.session_date, body.venue, body.duration_hours, body.notes)


@app.delete("/api/sessions/{session_id}")
def api_delete_session(session_id: int):
    return db.delete_session(session_id)


@app.post("/api/sessions/{session_id}/matches", status_code=201)
def api_log_match(session_id: int, body: NewMatch):
    return db.log_match(session_id, body.my_score, body.opp_score, body.points_to_win, body.notes)


@app.put("/api/matches/{match_id}")
def api_update_match(match_id: int, body: MatchUpdate):
    return db.update_match(match_id, body.my_score, body.opp_score, body.points_to_win, body.notes)


@app.delete("/api/matches/{match_id}")
def api_delete_match(match_id: int):
    return db.delete_match(match_id)


@app.get("/api/venues")
def api_list_venues():
    return db.list_venues()


@app.get("/api/stats/overall")
def api_overall_stats(start_date: str | None = None, end_date: str | None = None):
    return db.get_overall_stats(start_date, end_date)


@app.get("/api/stats/venues")
def api_venue_stats():
    return db.get_venue_stats()


@app.get("/api/stats/trend")
def api_monthly_trend(months: int = 12):
    return db.get_monthly_trend(months)


@app.get("/api/sample-data")
def api_sample_data_status():
    return {"has_sample_data": db.has_sample_data()}


@app.delete("/api/sample-data")
def api_delete_sample_data():
    return db.delete_sample_data()


@app.get("/")
def serve_index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/mcp", mcp_app)
