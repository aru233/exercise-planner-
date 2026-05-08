# Exercise Planner

A small, end-to-end demo of an [MCP](https://modelcontextprotocol.io/)
server with a Gemini-driven agent on top and a live [Prefab](https://github.com/anthropics/prefab-ui)
dashboard in the browser. You answer a few questions in the terminal, the
agent picks tools and runs them, and the dashboard fills in front of you.

It pulls real exercises from the public [wger](https://wger.de/) API,
keeps a local log of what you actually did, computes stats, and renders a
4-tab dashboard (Overview / Today / History / Stats) that the agent
updates after every tool call.

```
┌──────────────────┐     stdio MCP    ┌──────────────────────────┐
│  agent (Gemini)  │ ───────────────▶ │  exercise_planner_server │
│  + live_demo.py  │ ◀─────────────── │  • fetch_exercise_plan   │
└──────────────────┘   tool results   │  • manage_workout_log    │
        │                             │  • get_stats             │
        │ rewrites _dashboard.py      │  • render_planner        │
        ▼                             └──────────────────────────┘
┌──────────────────┐
│  prefab serve    │ ◀─── browser
│  _dashboard.py   │      auto-reloads
└──────────────────┘      after each tool call
```

## Quick start

```bash
git clone <this-repo> exercise_planner
cd exercise_planner

# 1. set up the venv + install deps + seed .env
bash setup_venv.sh
source .venv/bin/activate

# 2. drop your Gemini key into .env
#    (get one at https://aistudio.google.com/app/apikey)
$EDITOR .env

# 3. run the full live demo
python live_demo.py
```

The first run opens an empty Prefab dashboard in your browser, asks you a
few questions in the terminal (goal, days/week, equipment, today's
session), then watches the agent populate the dashboard live.

## The MCP tools

The server exposes four tools. Each maps to one of the three building
blocks an agent typically needs: reach the internet, persist state
locally, and surface results in a UI.

| Tool                    | Category          | What it does |
|-------------------------|-------------------|---------------------------------------------------------------------------|
| `fetch_exercise_plan`   | Internet          | Calls the public **wger** exercise API (no auth required), filters by goal / days-per-week / equipment, composes a multi-day split (Full Body / Upper-Lower / PPL), and writes `data/current_plan.json`. |
| `manage_workout_log`    | Local file CRUD   | `list` / `read` / `create` / `update` / `delete` against `data/workout_log.json`. Auto-stamps `id` and `date` on create. |
| `get_stats`             | Compute           | Pure rollup over the log: streak, sessions, total volume, weekly volume, per-muscle-group volume, personal records, average RPE. |
| `render_planner`        | UI (always on)    | Builds a JSON dashboard spec (Overview / Today / History / Stats) and writes `data/last_dashboard.json`. Any front-end can consume it. |
| `render_planner_ui`     | UI (Prefab-aware) | Auto-registered if `prefab-ui` imports cleanly. Returns a real `PrefabApp` so a Prefab-aware MCP host (`fastmcp dev apps`) can render it inline. |

Every tool wraps its body in a `log_call(...)` context manager that emits
a numbered header, an input arrow, indented `[step a/b]` lines for each
sub-step, and a result arrow with timing. The same lines are flushed to
`logs/server.log` so a third party can replay what the agent did.

## Repo layout

```
exercise_planner/
├── README.md                    ← this file
├── requirements.txt
├── setup_venv.sh                ← creates .venv, installs deps, seeds .env
├── .env.example                 ← template for GEMINI_API_KEY + GEMINI_MODEL
├── .gitignore
│
├── exercise_planner_server.py   ← the MCP server (4 tools + optional Prefab UI tool)
├── live_demo.py                 ← end-to-end demo: dashboard + interactive prompts + agent
├── _dashboard.py                ← (generated) Prefab dashboard, rewritten after each tool call
│
├── data/                        ← created at first run
│   ├── current_plan.json        ← written by fetch_exercise_plan
│   ├── workout_log.json         ← written by manage_workout_log
│   └── last_dashboard.json      ← written by render_planner
│
└── logs/
    ├── server.log               ← every MCP tool call, every sub-step
    └── prefab_server.log        ← stdout/stderr of the prefab serve subprocess
```

## Run modes

### 1. Full live demo — `live_demo.py`

The interesting one. Run this first.

```bash
python live_demo.py
```

What happens, in order:

1. **Empty dashboard opens.** `_dashboard.py` is regenerated with empty
   state, `prefab serve --reload` is spawned as a subprocess, and your
   browser opens to `http://127.0.0.1:5175` (or whatever port Prefab
   picked — the script reads the actual URL from the Prefab log).
2. **Terminal asks you what you want.** Goal (strength / hypertrophy /
   endurance), days per week, equipment, and optionally a session to
   log. Press Enter at any prompt to accept the default.
3. **Agent runs.** Your answers become a natural-language task; Gemini
   picks tools one at a time, the script executes them, the result feeds
   back into the next prompt. Loop ends on `FINAL_ANSWER`.
4. **Dashboard refreshes after every tool call.** `_dashboard.py` is
   rewritten with the latest state baked in as Python literals; on
   macOS, an AppleScript snippet reloads the Chrome tab so you watch
   the dashboard fill in.
5. **One safety-net tab at the end.** After `FINAL_ANSWER`, the script
   opens one fresh tab at the dashboard URL, guaranteeing you see the
   final populated state even if the in-flight reloads were flaky.

You'll see two streams of output during the run:

- **Terminal** — `--- Iteration N ---`, `LLM: FUNCTION_CALL: ...`,
  `→ tool(args)`, `← result`.
- **`logs/server.log`** — `--- Tool call #N: tool_name ---`, indented
  `[step a/b]` lines, the result arrow with timing in ms. This is the
  artefact most worth reading; it tells the story of the agent at work.

CLI flags:

```
--keep-data    Don't wipe data/*.json at startup (default: wipe so the
               dashboard truly starts empty).
```

### 2. Browser preview of the UI tool — `fastmcp dev apps`

Skips the agent and lets you click each tool yourself in a browser. Good
for sanity-checking that `render_planner_ui` actually renders.

```bash
fastmcp dev apps exercise_planner_server.py
```

### 3. Bare MCP server — for a third-party host

Run the server as a normal stdio MCP server and wire it into Claude
Desktop, Cursor, or any other MCP host.

```bash
python exercise_planner_server.py
```

Tail the log in another terminal:

```bash
tail -f logs/server.log
```

## Configuration

Drop these into `.env` (start from `.env.example`):

| Variable          | Purpose                                                                |
|-------------------|------------------------------------------------------------------------|
| `GEMINI_API_KEY`  | Required for `live_demo.py`. Get one at [aistudio.google.com](https://aistudio.google.com/app/apikey). |
| `GEMINI_MODEL`    | Defaults to `gemini-2.5-flash-lite`. `gemini-2.5-flash` has more headroom on the free tier if you hit quotas. |

The bare MCP server has no required environment variables.

## How it works

**MCP server (`exercise_planner_server.py`).** Built on
[`fastmcp`](https://github.com/jlowin/fastmcp). Each `@mcp.tool()`
function is a normal Python function whose signature becomes the JSON
schema the agent sees. The `@mcp.tool(app=True)` decorator on
`render_planner_ui` is a Prefab-specific extension that lets the tool
return a `PrefabApp` instead of a string.

**Agent loop (`live_demo.py`).** Pure ReAct: send the
task and the tool catalogue to Gemini, parse a single-line
`FUNCTION_CALL: name|arg1|arg2|...` reply, coerce the args back to
typed Python values, dispatch over MCP stdio, append the result to the
running history, repeat. Stop on `FINAL_ANSWER`.

**Dashboard refresh (`live_demo.py`).** Three layers:

1. After each tool call, `_dashboard.py` is rewritten with the latest
   `current_plan.json` + `workout_log.json` baked in as Python
   literals, so its content actually differs every time.
2. `prefab serve --reload` watches that file and re-imports it.
3. On macOS, an AppleScript snippet asks Chrome to reload the
   dashboard tab so the new content is visible without a manual refresh.

If you're not on macOS, step 3 is a no-op and you'll need to refresh
the tab yourself after each tool call (or wait for the safety-net tab
at the end). PRs welcome to add a Linux / Windows equivalent.

## Sample log shape

```
[2026-05-09 14:02:11] ===== server start =====
[2026-05-09 14:02:11] [info] data dir : .../exercise_planner/data
[2026-05-09 14:02:11] [info] tools registered: fetch_exercise_plan, manage_workout_log, get_stats, render_planner, render_planner_ui

[2026-05-09 14:02:34] --- Tool call #1: fetch_exercise_plan ---
[2026-05-09 14:02:34] → fetch_exercise_plan(goal="hypertrophy", days_per_week=4, equipment=["Dumbbell","Barbell"])
[2026-05-09 14:02:34]   [step 1/4] fetching exercises from wger ...
[2026-05-09 14:02:34]   [wger] HTTP GET https://wger.de/api/v2/exerciseinfo/?language=2&limit=200
[2026-05-09 14:02:35]   [wger] HTTP 200 (412813 bytes)
[2026-05-09 14:02:35]   [wger] parsed 200 exercise records
[2026-05-09 14:02:35]   [step 2/4] normalising exercise records ...
[2026-05-09 14:02:35]   [step 2/4] 184 usable records after normalise
[2026-05-09 14:02:35]   [step 3/4] composing the 4-day split ...
[2026-05-09 14:02:35]   [plan] using 4-day template (4 sessions)
[2026-05-09 14:02:35]   [plan] equipment filter ['barbell','dumbbell'] → 71/184
[2026-05-09 14:02:35]   [plan] tag buckets: arms=12, back=10, chest=15, ...
[2026-05-09 14:02:35]   [step 3/4] composed 4 days, 24 exercises total
[2026-05-09 14:02:35]   [step 4/4] writing data/current_plan.json ...
[2026-05-09 14:02:35]   [step 4/4] wrote 6402 bytes
[2026-05-09 14:02:35] ← fetch_exercise_plan ok in 1284 ms — {"plan_id":"plan_20260509_140235", ...}
```

## Design notes (for the curious)

- **`log_call` is one context manager**, not a decorator stack — so the
  numbered header, input arrow, and result arrow are guaranteed to be
  consistent across every tool. Sub-steps inside the tool just call
  `log_step(...)` and get an indented line for free.
- **All disk I/O is confined to `data/`.** A tiny sandbox pattern that
  keeps the server from reaching outside its own folder.
- **The Prefab UI tool is optional.** If `prefab-ui` isn't installed,
  the server still registers `render_planner` (the JSON-spec variant)
  and the agent loop runs fine — you just lose the live dashboard.
- **Dashboard data is embedded, not read at runtime.** Each refresh
  rewrites `_dashboard.py` with `repr(plan)` and `repr(entries)` baked
  in as module-level literals. This makes Prefab's file watcher
  reliable: the source is textually different every time, never
  "looks the same."
- **No subprocess restart loop.** An earlier version restarted
  `prefab serve` after each tool call to force a refresh, but on macOS
  the OS holds port 5175 in `TIME_WAIT` for ~30s and the new process
  can't bind. Embedding-data + AppleScript reload sidesteps the issue.

## Troubleshooting

**`mcp[cli]` won't install.** You're on Python < 3.10. `mcp` requires
3.10+. `setup_venv.sh` prefers `python3.12 / 3.11 / 3.10` if any are on
your PATH; install one (e.g. `brew install python@3.12`) and re-run.

**`429 RESOURCE_EXHAUSTED` from Gemini.** Free-tier daily quota. Either
wait 24 hours, or switch model in `.env`:

```
GEMINI_MODEL=gemini-2.5-flash
```

`gemini-2.5-flash` has a separate quota bucket from `flash-lite`.

**Browser opens at `:5176` instead of `:5175`.** Something else was on
port 5175 (probably a stale Prefab from a previous run). The script
detects the actual port by reading `logs/prefab_server.log` and uses
that throughout. To force 5175, kill the stale process first:

```bash
lsof -ti tcp:5175 | xargs kill -9 2>/dev/null
```

**Dashboard doesn't auto-refresh.** The AppleScript path is macOS +
Chrome only. On other setups, refresh the tab manually after each tool
call, or rely on the safety-net tab that opens at the end.

## License

MIT — do whatever, attribution appreciated but not required.
