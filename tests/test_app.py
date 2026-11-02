import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from fastmcp import Client

import database as db
from main import app, mcp

client = TestClient(app)


def _new_session(**overrides):
    body = {"session_date": "2026-10-01", "venue": "Sports Hub", "duration_hours": 2, **overrides}
    return client.post("/api/sessions", json=body)


# ---------- REST ----------

def test_session_and_match_crud_flow():
    res = _new_session()
    assert res.status_code == 201
    sid = res.json()["id"]
    assert res.json()["duration_hours"] == 2

    res = client.post(f"/api/sessions/{sid}/matches", json={"my_score": 21, "opp_score": 18})
    assert res.status_code == 201
    mid = res.json()["id"]
    assert res.json()["result"] == "win"

    res = client.put(f"/api/matches/{mid}", json={"notes": "edited"})
    assert res.status_code == 200 and res.json()["notes"] == "edited"

    res = client.put(f"/api/sessions/{sid}", json={"duration_hours": 3})
    assert res.status_code == 200 and res.json()["duration_hours"] == 3

    assert client.get(f"/api/sessions/{sid}").json()["matches"][0]["id"] == mid
    assert client.get("/api/stats/overall").json()["wins"] == 1

    assert client.delete(f"/api/matches/{mid}").status_code == 200
    assert client.delete(f"/api/sessions/{sid}").status_code == 200
    assert client.get(f"/api/sessions/{sid}").status_code == 404


def test_invalid_input_returns_400_with_message():
    res = _new_session(session_date="2026-02-30")
    assert res.status_code == 400
    assert "YYYY-MM-DD" in res.json()["detail"]

    assert _new_session(duration_hours=0).status_code == 400

    sid = _new_session().json()["id"]
    res = client.post(f"/api/sessions/{sid}/matches", json={"my_score": 21, "opp_score": 20})
    assert res.status_code == 400
    assert "21-20" in res.json()["detail"]


def test_unknown_ids_return_404():
    assert client.delete("/api/sessions/999").status_code == 404
    assert client.put("/api/matches/999", json={"notes": "x"}).status_code == 404
    res = client.post("/api/sessions/999/matches", json={"my_score": 21, "opp_score": 1})
    assert res.status_code == 404
    assert res.json()["detail"] == "session 999 not found"


def test_sample_data_endpoints():
    db.create_session("2026-09-01", "Old Hall", 1, _is_sample=True)
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


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize("provided", [
    {},
    {"session_date": "2026-10-12"},
    {"session_date": "2026-10-12", "venue": "Community Centre"},
    {"venue": "Community Centre", "duration_hours": 2},
    {"session_date": "2026-10-12", "venue": "Community Centre", "duration_hours": None},
    {"session_date": "2026-10-12", "venue": "  ", "duration_hours": 2},
])
def test_mcp_session_logging_requests_only_missing_fields_in_chat(mode, provided):
    details = {
        "session_date": "2026-10-12",
        "venue": "Community Centre",
        "duration_hours": 2,
    }

    async def log_via_mcp():
        async with Client(mcp, mode=mode) as c:
            result = await c.call_tool("log_session", {**provided, "notes": "Evening doubles"})
            clarification = result.structured_content
            missing = [
                key for key in details
                if provided.get(key) is None or (
                    isinstance(provided[key], str) and not provided[key].strip()
                )
            ]
            assert clarification["status"] == "needs_input"
            assert clarification["missing_fields"] == missing
            assert clarification["provided"] == {
                **{key: value for key, value in provided.items() if key not in missing},
                "notes": "Evening doubles",
            }
            assert set(clarification["questions"]) == set(missing)
            assert not result.is_error
            assert db.list_sessions() == []
            result = await c.call_tool("log_session", {
                **clarification["provided"],
                **{key: details[key] for key in missing},
            })
            assert result.structured_content["session_date"] == "2026-10-12"
            assert result.structured_content["venue"] == "Community Centre"
            assert result.structured_content["duration_hours"] == 2

    asyncio.run(log_via_mcp())
    sessions = client.get("/api/sessions").json()
    assert len(sessions) == 1
    assert sessions[0]["notes"] == "Evening doubles"


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_complete_mcp_session_does_not_elicit(mode):
    async def unexpected_form(*args):
        pytest.fail("A complete session must not request input.")

    async def log_complete_session():
        async with Client(mcp, mode=mode, elicitation_handler=unexpected_form) as c:
            result = await c.call_tool("log_session", {
                "session_date": "2026-11-02", "venue": "Sports Hub", "duration_hours": 2,
            })
            assert result.structured_content["notes"] == ""

    asyncio.run(log_complete_session())
    assert len(db.list_sessions()) == 1


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_invalid_clarification_answer_does_not_write(mode):
    async def try_invalid_session():
        async with Client(mcp, mode=mode) as c:
            partial = await c.call_tool(
                "log_session", {"venue": "Sports Hub", "duration_hours": 2},
            )
            assert partial.structured_content["missing_fields"] == ["session_date"]
            result = await c.call_tool(
                "log_session", {
                    **partial.structured_content["provided"], "session_date": "2026-02-30",
                },
                raise_on_error=False,
            )
            assert result.is_error
            assert "YYYY-MM-DD" in result.content[0].text

    asyncio.run(try_invalid_session())
    assert db.list_sessions() == []


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_unanswered_clarification_does_not_write(mode):
    async def try_without_accepting():
        async with Client(mcp, mode=mode) as c:
            result = await c.call_tool("log_session", {"venue": "Sports Hub"})
            assert result.structured_content["status"] == "needs_input"

    asyncio.run(try_without_accepting())
    assert client.get("/api/sessions").json() == []


def test_mcp_http_endpoint_works_with_and_without_trailing_slash():
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    headers = {"Accept": "application/json, text/event-stream"}
    with TestClient(app) as c:  # runs the MCP lifespan
        for path in ("/mcp", "/mcp/"):
            res = c.post(path, json=init, headers=headers, follow_redirects=False)
            assert res.status_code == 200, path
            assert "Badminton Tracker" in res.text


def test_stateless_mcp_http_chat_clarification_without_back_channel():
    headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    }

    def call_tool(http_client, arguments):
        response = http_client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "log_session", "arguments": arguments},
        })
        assert response.status_code == 200
        if response.headers["content-type"].startswith("text/event-stream"):
            messages = [
                json.loads(line[5:].strip())
                for line in response.text.splitlines() if line.startswith("data:")
            ]
            assert len(messages) == 1
            payload = messages[0]
        else:
            payload = response.json()
        assert "error" not in payload
        assert not payload["result"].get("isError", False)
        return payload["result"]["structuredContent"]

    with TestClient(app) as http_client:
        partial = call_tool(http_client, {
            "session_date": "2026-11-02", "venue": "Sports Hub", "notes": "Doubles",
        })
        assert partial["status"] == "needs_input"
        assert partial["missing_fields"] == ["duration_hours"]
        assert partial["questions"] == {
            "duration_hours": "How many whole hours did you play (1 to 24)?",
        }
        assert db.list_sessions() == []
        complete = call_tool(http_client, {**partial["provided"], "duration_hours": 2})
        assert complete["duration_hours"] == 2
        assert complete["notes"] == "Doubles"
    assert len(db.list_sessions()) == 1


def test_mcp_logging_is_visible_to_rest_api():
    async def log_via_mcp():
        async with Client(mcp) as c:
            session = await c.call_tool("log_session",
                                        {"session_date": "2026-10-10", "venue": "AI Court", "duration_hours": 2})
            sid = session.structured_content["id"]
            await c.call_tool("log_match", {"session_id": sid, "my_score": 21, "opp_score": 10})
            await c.call_tool("log_match", {"session_id": sid, "my_score": 15, "opp_score": 21})
            bad = await c.call_tool("log_match", {"session_id": sid, "my_score": 21, "opp_score": 20},
                                    raise_on_error=False)
            assert bad.is_error

    asyncio.run(log_via_mcp())
    sessions = client.get("/api/sessions").json()
    assert sessions[0]["venue"] == "AI Court"
    assert [m["score"] for m in sessions[0]["matches"]] == ["21-10", "15-21"]
    assert client.get("/api/venues").json() == ["AI Court"]
