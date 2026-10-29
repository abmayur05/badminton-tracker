# Badminton Tracker

A personal badminton tracker. Log play sessions and match scores in a browser,
or ask an AI assistant to do it over MCP — both use the same SQLite database.

## Run it

```powershell
uv run python -m uvicorn main:app --reload
```

- Website: <http://127.0.0.1:8000>
- MCP endpoint (Streamable HTTP): <http://127.0.0.1:8000/mcp>

> Use `python -m uvicorn` / `python -m pytest` rather than `uv run uvicorn` /
> `uv run pytest`: launching `.exe` files from this OneDrive folder is blocked
> ("Access is denied").

### First-time setup (only if `.venv` is missing)

```powershell
$env:UV_LINK_MODE = "copy"   # OneDrive doesn't support uv's hardlinks
uv sync --native-tls          # --native-tls trusts the corporate TLS proxy certificate
```

## What it tracks

| Thing | Fields |
|---|---|
| Session | date, venue, duration (minutes), notes |
| Match | best of 3 games, games to 21 (default), 15, or 11, notes |
| Game | your score and the opponents' score |

The win/loss result is **calculated from the scores**, never typed in. Scores are
validated with rally-scoring rules: win by 2, capped at target + 9
(21 → 30, so `30-29` is valid but `21-20` and `25-15` are not).

### Sample data

Three sample sessions are added on first run and marked **sample**. Remove them
with **Delete sample data** on the website (or the `delete_sample_data` MCP
tool). Your own sessions are untouched, and samples are never re-added.

## Website

- **Sessions** — every session with its matches; edit or delete any of them
- **Log** — add a session, then add matches to it
- **Stats** — overall record, win % by venue, monthly trend

## MCP

| Primitive | Name | What it does |
|---|---|---|
| Tool | `log_session` / `log_match` | Record a session or a match |
| Tool | `update_session` / `update_match` | Edit — only the fields passed change |
| Tool | `delete_session` / `delete_match` | Delete (a session takes its matches with it) |
| Tool | `list_sessions` / `get_session` | Browse history, filter by dates or venue |
| Tool | `get_overall_stats` | Record, win %, games and points — optional date range |
| Tool | `get_venue_stats` / `get_monthly_trend` | Win % by venue; month-by-month trend |
| Tool | `delete_sample_data` | Remove the seeded sample sessions |
| Resource | `badminton://venues` | Venues already used, for consistent naming |
| Prompt | `weekly_recap` | Builds a weekly performance recap |

Connect an MCP client such as Claude Desktop:

```json
{
  "mcpServers": {
    "badminton": { "url": "http://127.0.0.1:8000/mcp" }
  }
}
```

## REST API

| Method | Path |
|---|---|
| `GET` / `POST` | `/api/sessions` (`GET` accepts `start_date`, `end_date`, `venue`) |
| `GET` / `PUT` / `DELETE` | `/api/sessions/{id}` |
| `POST` | `/api/sessions/{id}/matches` |
| `PUT` / `DELETE` | `/api/matches/{id}` |
| `GET` | `/api/venues`, `/api/stats/overall`, `/api/stats/venues`, `/api/stats/trend` |
| `GET` / `DELETE` | `/api/sample-data` |

Invalid input returns `400` with a readable `detail`; unknown ids return `404`.

## Tests

```powershell
uv run python -m pytest
```

Tests use a temporary database, never your real `badminton.db`.

## Files

- `database.py` — SQLite schema, score validation, CRUD, and stats
- `main.py` — FastAPI app with the MCP server mounted at `/mcp`
- `static/` — the website
- `tests/` — database, REST, and MCP tests
