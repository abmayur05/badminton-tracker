import asyncio

from fastapi.testclient import TestClient
from fastmcp import Client

import database as db
from main import app, mcp

client = TestClient(app)


def _new_session(**overrides):
    body = {"session_date": "2026-10-01", "venue": "Sports Hub", "duration_minutes": 90, **overrides}
    return client.post("/api/sessions", json=body)


# ---------- REST ----------

def test_session_and_match_crud_flow():
    res = _new_session()
    assert res.status_code == 201
    sid = res.json()["id"]

    res = client.post(f"/api/sessions/{sid}/matches",
                      json={"games": [{"my_score": 21, "opp_score": 18}, {"my_score": 21, "opp_score": 15}]})
    assert res.status_code == 201
    mid = res.json()["id"]
    assert res.json()["result"] == "win"

    res = client.put(f"/api/matches/{mid}", json={"notes": "edited"})
    assert res.status_code == 200 and res.json()["notes"] == "edited"

    res = client.put(f"/api/sessions/{sid}", json={"duration_minutes": 120})
    assert res.status_code == 200 and res.json()["duration_minutes"] == 120

    assert client.get(f"/api/sessions/{sid}").json()["matches"][0]["id"] == mid
    assert client.get("/api/stats/overall").json()["wins"] == 1

    assert client.delete(f"/api/matches/{mid}").status_code == 200
    assert client.delete(f"/api/sessions/{sid}").status_code == 200
    assert client.get(f"/api/sessions/{sid}").status_code == 404


def test_invalid_input_returns_400_with_message():
    res = _new_session(session_date="2026-02-30")
    assert res.status_code == 400
    assert "YYYY-MM-DD" in res.json()["detail"]

    sid = _new_session().json()["id"]
    res = client.post(f"/api/sessions/{sid}/matches",
                      json={"games": [{"my_score": 21, "opp_score": 20}, {"my_score": 21, "opp_score": 15}]})
    assert res.status_code == 400
    assert "21-20" in res.json()["detail"]


def test_unknown_ids_return_404():
    assert client.delete("/api/sessions/999").status_code == 404
    assert client.put("/api/matches/999", json={"notes": "x"}).status_code == 404
    res = client.post("/api/sessions/999/matches",
                      json={"games": [{"my_score": 21, "opp_score": 1}, {"my_score": 21, "opp_score": 1}]})
    assert res.status_code == 404
    assert res.json()["detail"] == "session 999 not found"


def test_sample_data_endpoints():
    db.create_session("2026-09-01", "Old Hall", 60, _is_sample=True)
    assert client.get("/api/sample-data").json() == {"has_sample_data": True}
    assert client.delete("/api/sample-data").json() == {"deleted_sessions": 1}
    assert client.get("/api/sample-data").json() == {"has_sample_data": False}


def test_index_page_served():
    res = client.get("/")
    assert res.status_code == 200
    assert "Badminton Tracker" in res.text


# ---------- MCP ----------

def test_mcp_exposes_tools_resource_and_prompt():
    async def check():
        async with Client(mcp) as c:
            tools = {t.name for t in await c.list_tools()}
            assert {"log_session", "log_match", "update_match", "delete_match", "update_session",
                    "delete_session", "list_sessions", "get_session", "get_overall_stats",
                    "get_venue_stats", "get_monthly_trend", "delete_sample_data"} <= tools
            assert "badminton://venues" in {str(r.uri) for r in await c.list_resources()}
            assert "weekly_recap" in {p.name for p in await c.list_prompts()}

    asyncio.run(check())


def test_mcp_logging_is_visible_to_rest_api():
    async def log_via_mcp():
        async with Client(mcp) as c:
            session = await c.call_tool("log_session",
                                        {"session_date": "2026-10-10", "venue": "AI Court", "duration_minutes": 60})
            await c.call_tool("log_match", {
                "session_id": session.structured_content["id"],
                "games": [{"my_score": 21, "opp_score": 10}, {"my_score": 21, "opp_score": 12}],
            })
            bad = await c.call_tool("log_match", {
                "session_id": session.structured_content["id"],
                "games": [{"my_score": 21, "opp_score": 20}, {"my_score": 21, "opp_score": 12}],
            }, raise_on_error=False)
            assert bad.is_error

    asyncio.run(log_via_mcp())
    sessions = client.get("/api/sessions").json()
    assert sessions[0]["venue"] == "AI Court"
    assert sessions[0]["matches"][0]["score"] == "21-10, 21-12"
    assert client.get("/api/venues").json() == ["AI Court"]
