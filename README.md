# Badminton Tracker

A personal badminton tracker. Log play sessions and match scores in a browser,
or ask an AI assistant to do it over MCP — both use the same SQLite database.

## Run it

```powershell
uv run python -m uvicorn main:app --reload
```

- Website: <http://127.0.0.1:8000>
- MCP endpoint (Streamable HTTP): <http://127.0.0.1:8000/mcp>

### First-time setup

```powershell
uv sync
```

If you're behind a corporate TLS proxy, use `uv sync --native-tls`.

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

## Deploy to Vercel

Vercel's filesystem is read-only and temporary, so the hosted app stores data in
[Turso](https://turso.tech) (hosted SQLite, free tier) instead of `badminton.db`.
The app switches automatically when `TURSO_DATABASE_URL` is set.

### 1. Create the Turso database

1. Sign in at <https://app.turso.tech> (GitHub login works).
2. **Create Database** → name it `badminton-tracker` → pick the region closest to
   your Vercel functions (Vercel's default is Washington, D.C. — AWS `us-east-1`).
3. Open the database and copy its **URL** (starts with `libsql://`).
4. **Create Token** → read & write → copy the token. It's shown only once.

### 2. Deploy on Vercel

1. Sign in at <https://vercel.com> with GitHub.
2. **Add New → Project** → import the `badminton-tracker` repository.
   Vercel detects FastAPI (`main.py` → `app`) and Python 3.13 automatically.
3. Under **Environment Variables**, add:

   | Name | Value |
   |---|---|
   | `TURSO_DATABASE_URL` | the `libsql://…` URL |
   | `TURSO_AUTH_TOKEN` | the token |

4. **Deploy**. Tables and sample data are created on the first request.

Your app is at `https://<project>.vercel.app`, and the MCP endpoint at
`https://<project>.vercel.app/mcp`. Every push to `main` redeploys automatically.

> **Note:** the app has no login. Anyone with the URL can view, edit, and delete
> data — through the website or MCP. Keep the URL private, or enable
> **Vercel Authentication** under *Settings → Deployment Protection*
> (this also blocks MCP clients that can't sign in).

### Try Turso locally first (optional)

```powershell
$env:TURSO_DATABASE_URL = "libsql://<your-db>.turso.io"
$env:TURSO_AUTH_TOKEN = "<token>"
uv run python -m uvicorn main:app --reload
```

## Files

- `database.py` — schema, score validation, CRUD, and stats (local SQLite file or Turso)
- `main.py` — FastAPI app with the MCP server mounted at `/mcp`
- `static/` — the website
- `tests/` — database, REST, and MCP tests
