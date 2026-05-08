"""
Exercise Planner — an MCP server with four tools.

The server is built around three capabilities:

  1. INTERNET           — `fetch_exercise_plan` calls the public wger API
                           (no auth needed) and assembles a multi-day workout
                           split.
  2. LOCAL FILE CRUD    — `manage_workout_log` does create / read / update /
                           delete / list against `data/workout_log.json`.
                           `fetch_exercise_plan` also writes
                           `data/current_plan.json`.
  3. UI BACK TO USER    — `render_planner` returns a Prefab `PrefabApp` with
                           four tabs (Overview / Today / History / Stats).
                           A bonus tool `get_stats` is the data source.

The interesting bit is the logging.  Every tool call prints a numbered
"--- Tool call #N ---" header, an arrow line for the inputs, indented
[step a/b] lines for each sub-step, and an arrow line for the result.
The same lines are flushed to `logs/server.log` so you can replay what
the agent did after the fact.

Run:
    python exercise_planner_server.py            # stdio (for an MCP host)
    fastmcp dev apps exercise_planner_server.py  # browser preview (UI tool)
"""

from __future__ import annotations

import json
import os
import sys
import time
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Any, Iterable

import requests
# We deliberately use the standalone `fastmcp` package (not
# `mcp.server.fastmcp`) because Prefab's `@mcp.tool(app=True)` integration
# only exists on the standalone one.  Both expose the same MCP protocol,
# so the standard `mcp` client used by live_demo.py talks to it just fine.
from fastmcp import FastMCP


# ---------------------------------------------------------------------------
# Paths & sandbox.  Everything we touch on disk lives under HERE.
# ---------------------------------------------------------------------------

HERE = Path(__file__).parent
DATA_DIR = HERE / "data"
LOG_DIR = HERE / "logs"
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

PLAN_FILE = DATA_DIR / "current_plan.json"
LOG_FILE = DATA_DIR / "workout_log.json"
SERVER_LOG = LOG_DIR / "server.log"


# ---------------------------------------------------------------------------
# Logger.  Two design goals:
#   1. The log should READ like a story of the agent at work — numbered
#      tool calls, indented sub-steps, arrows for input/output.
#   2. Output goes to BOTH the file (so the trace is replayable) and
#      stderr (so the MCP host shows it live).
# ---------------------------------------------------------------------------

_log_lock = threading.Lock()
_call_counter = {"n": 0}


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _emit(line: str) -> None:
    """Write one line to stderr AND append to logs/server.log."""
    with _log_lock:
        print(line, file=sys.stderr, flush=True)
        with SERVER_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def _short(value: Any, limit: int = 240) -> str:
    """Pretty-print but truncate so logs stay readable."""
    try:
        s = json.dumps(value, default=str)
    except Exception:
        s = str(value)
    return s if len(s) <= limit else s[:limit] + "..."


def log_step(message: str, *, indent: int = 2) -> None:
    """Emit an indented sub-step line, e.g. '  [step 1/3] HTTP GET ...'."""
    _emit(f"[{_ts()}]{' ' * indent}{message}")


@contextmanager
def log_call(tool_name: str, **kwargs: Any):
    """Context manager that wraps a tool body.

    Emits a '--- Tool call #N ---' header, the '→' input arrow, and on
    exit either a '←' result arrow or a '✗' error line.  Sub-steps inside
    the body call `log_step(...)`.
    """
    with _log_lock:
        _call_counter["n"] += 1
        n = _call_counter["n"]
    args_pretty = ", ".join(f"{k}={_short(v, 80)}" for k, v in kwargs.items())
    _emit("")
    _emit(f"[{_ts()}] --- Tool call #{n}: {tool_name} ---")
    _emit(f"[{_ts()}] → {tool_name}({args_pretty})")
    started = time.time()
    box = {"result": None}
    try:
        yield box
    except Exception as e:
        elapsed_ms = int((time.time() - started) * 1000)
        _emit(f"[{_ts()}] ✗ {tool_name} failed after {elapsed_ms} ms: {e!r}")
        raise
    else:
        elapsed_ms = int((time.time() - started) * 1000)
        _emit(f"[{_ts()}] ← {tool_name} ok in {elapsed_ms} ms — {_short(box['result'])}")


# ---------------------------------------------------------------------------
# JSON-on-disk helpers.  Tiny but lifted out so the CRUD tool stays clean.
# ---------------------------------------------------------------------------

def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> int:
    """Write JSON, return number of bytes."""
    payload = json.dumps(value, indent=2, default=str)
    path.write_text(payload, encoding="utf-8")
    return len(payload)


# ---------------------------------------------------------------------------
# wger client.  We only need one endpoint: /exerciseinfo/  (translated, with
# category + equipment + images joined).  We fetch once, filter in Python.
# ---------------------------------------------------------------------------

WGER_BASE = "https://wger.de/api/v2"
WGER_LANGUAGE_EN = 2  # the wger language id for English


def _wger_fetch_exercises(limit: int = 200) -> list[dict]:
    """GET /exerciseinfo/ — returns a list of dicts ready to filter."""
    url = f"{WGER_BASE}/exerciseinfo/?language={WGER_LANGUAGE_EN}&limit={limit}"
    log_step(f"[wger] HTTP GET {url}")
    r = requests.get(url, timeout=15)
    log_step(f"[wger] HTTP {r.status_code} ({len(r.content)} bytes)")
    r.raise_for_status()
    payload = r.json()
    results = payload.get("results", [])
    log_step(f"[wger] parsed {len(results)} exercise records")
    return results


# Category-id → tag mapping.  wger ids are stable for these.
_CATEGORY_TAG = {
    8: "arms",
    9: "back",
    10: "calves",
    11: "chest",
    12: "legs",
    13: "shoulders",
    14: "other",
    15: "abs",
}


def _exercise_summary(ex: dict) -> dict:
    """Pull the bits we care about out of a wger record."""
    # Pick the English translation if there is one, else the first.
    name = ""
    for tr in ex.get("translations", []) or []:
        if tr.get("language") == WGER_LANGUAGE_EN and tr.get("name"):
            name = tr["name"]
            break
    if not name:
        # Older wger payloads use a top-level `name`.
        name = ex.get("name") or "Unnamed exercise"
    cat = ex.get("category") or {}
    cat_id = cat.get("id") if isinstance(cat, dict) else None
    cat_name = cat.get("name") if isinstance(cat, dict) else ""
    equipment = [e.get("name") for e in (ex.get("equipment") or []) if isinstance(e, dict)]
    images = [img.get("image") for img in (ex.get("images") or []) if isinstance(img, dict)]
    return {
        "id": ex.get("id"),
        "name": name,
        "category_id": cat_id,
        "category": cat_name,
        "tag": _CATEGORY_TAG.get(cat_id, (cat_name or "").lower()),
        "equipment": equipment,
        "image": images[0] if images else None,
    }


# Day-template for each split.  Keys are tags, values are how many exercises
# we want from that tag for that day.
_SPLIT_TEMPLATES: dict[int, list[tuple[str, dict[str, int]]]] = {
    3: [
        ("Full Body A", {"chest": 1, "back": 1, "legs": 2, "shoulders": 1, "arms": 1}),
        ("Full Body B", {"chest": 2, "back": 2, "legs": 1, "shoulders": 1}),
        ("Full Body C", {"chest": 1, "back": 1, "legs": 2, "arms": 2}),
    ],
    4: [
        ("Upper A", {"chest": 2, "back": 2, "shoulders": 1, "arms": 1}),
        ("Lower A", {"legs": 3, "calves": 1, "abs": 1}),
        ("Upper B", {"chest": 2, "back": 2, "shoulders": 1, "arms": 1}),
        ("Lower B", {"legs": 3, "calves": 1, "abs": 1}),
    ],
    5: [
        ("Push", {"chest": 2, "shoulders": 2, "arms": 1}),
        ("Pull", {"back": 3, "arms": 2}),
        ("Legs", {"legs": 3, "calves": 1, "abs": 1}),
        ("Upper", {"chest": 1, "back": 2, "shoulders": 1, "arms": 1}),
        ("Lower", {"legs": 2, "calves": 1, "abs": 2}),
    ],
    6: [
        ("Push A", {"chest": 2, "shoulders": 2, "arms": 1}),
        ("Pull A", {"back": 3, "arms": 2}),
        ("Legs A", {"legs": 3, "calves": 1, "abs": 1}),
        ("Push B", {"chest": 2, "shoulders": 2, "arms": 1}),
        ("Pull B", {"back": 3, "arms": 2}),
        ("Legs B", {"legs": 3, "calves": 1, "abs": 1}),
    ],
}


def _build_split(
    exercises: list[dict],
    days_per_week: int,
    equipment: list[str] | None,
    goal: str,
) -> list[dict]:
    """Group exercises into days based on the split template."""
    template = _SPLIT_TEMPLATES.get(days_per_week)
    if template is None:
        # Fallback: use the closest defined template.
        template = _SPLIT_TEMPLATES[min(_SPLIT_TEMPLATES, key=lambda d: abs(d - days_per_week))]
        log_step(
            f"[plan] no template for {days_per_week} days → using nearest "
            f"({len(template)}-day split)"
        )
    else:
        log_step(f"[plan] using {days_per_week}-day template ({len(template)} sessions)")

    # Filter by equipment if requested.
    if equipment:
        wanted = {e.lower() for e in equipment}

        def matches(ex: dict) -> bool:
            ex_eq = {e.lower() for e in (ex.get("equipment") or []) if e}
            # An exercise with no equipment at all is bodyweight — always include
            # if 'none' or 'bodyweight' is wanted, otherwise drop it unless the
            # user wanted bodyweight too.
            if not ex_eq:
                return any(w in {"none", "bodyweight"} for w in wanted)
            return bool(ex_eq & wanted)

        before = len(exercises)
        exercises = [ex for ex in exercises if matches(ex)]
        log_step(f"[plan] equipment filter {sorted(wanted)} → {len(exercises)}/{before}")

    # Bucket exercises by tag.
    by_tag: dict[str, list[dict]] = {}
    for ex in exercises:
        by_tag.setdefault(ex.get("tag") or "other", []).append(ex)
    log_step(
        "[plan] tag buckets: "
        + ", ".join(f"{k}={len(v)}" for k, v in sorted(by_tag.items()))
    )

    # Sets/reps depend on the goal.
    sets_reps = {
        "strength": {"sets": 5, "reps": 5},
        "hypertrophy": {"sets": 3, "reps": 10},
        "endurance": {"sets": 3, "reps": 15},
    }.get(goal.lower(), {"sets": 3, "reps": 10})

    days: list[dict] = []
    cursor: dict[str, int] = {}
    for day_name, mix in template:
        picks: list[dict] = []
        for tag, count in mix.items():
            pool = by_tag.get(tag, [])
            i = cursor.get(tag, 0)
            for _ in range(count):
                if not pool:
                    break
                picks.append(pool[i % len(pool)])
                i += 1
            cursor[tag] = i
        # Stamp set/rep targets onto each pick.
        day_exercises = [
            {
                "name": p["name"],
                "tag": p["tag"],
                "category": p["category"],
                "equipment": p["equipment"],
                "image": p["image"],
                "target_sets": sets_reps["sets"],
                "target_reps": sets_reps["reps"],
            }
            for p in picks
        ]
        days.append({"day": day_name, "exercises": day_exercises})
    return days


# ===========================================================================
# MCP server
# ===========================================================================

mcp = FastMCP("ExercisePlanner")


# ---------------------------------------------------------------------------
# Tool 1 — INTERNET: build a workout plan from the wger API
# ---------------------------------------------------------------------------

@mcp.tool()
def fetch_exercise_plan(
    goal: str = "hypertrophy",
    days_per_week: int = 4,
    equipment: list[str] | None = None,
) -> dict:
    """Build a multi-day workout plan from the wger exercise API.

    Args:
        goal: 'strength', 'hypertrophy', or 'endurance'.
        days_per_week: 3, 4, 5, or 6 (other values fall back to the closest).
        equipment: e.g. ["Dumbbell", "Barbell"]. Pass None or [] for no filter.

    Returns:
        A small summary dict.  The full plan is also written to
        data/current_plan.json so the UI tool can read it.
    """
    with log_call(
        "fetch_exercise_plan",
        goal=goal,
        days_per_week=days_per_week,
        equipment=equipment,
    ) as box:
        log_step("[step 1/4] fetching exercises from wger ...")
        raw = _wger_fetch_exercises(limit=200)

        log_step("[step 2/4] normalising exercise records ...")
        summarised = [_exercise_summary(ex) for ex in raw]
        # Keep only records that have a name and a known tag.
        summarised = [s for s in summarised if s["name"] and s["tag"]]
        log_step(f"[step 2/4] {len(summarised)} usable records after normalise")

        log_step(f"[step 3/4] composing the {days_per_week}-day split ...")
        days = _build_split(summarised, days_per_week, equipment, goal)
        total_ex = sum(len(d["exercises"]) for d in days)
        log_step(f"[step 3/4] composed {len(days)} days, {total_ex} exercises total")

        plan = {
            "plan_id": f"plan_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "goal": goal,
            "days_per_week": days_per_week,
            "equipment": equipment or [],
            "days": days,
        }

        log_step("[step 4/4] writing data/current_plan.json ...")
        n_bytes = _write_json(PLAN_FILE, plan)
        log_step(f"[step 4/4] wrote {n_bytes} bytes")

        result = {
            "plan_id": plan["plan_id"],
            "goal": goal,
            "days_per_week": days_per_week,
            "exercises_total": total_ex,
            "first_day_preview": [e["name"] for e in days[0]["exercises"]] if days else [],
        }
        box["result"] = result
        return result


# ---------------------------------------------------------------------------
# Tool 2 — LOCAL CRUD: workout log (create/read/update/delete/list)
# ---------------------------------------------------------------------------

def _load_log() -> list[dict]:
    return _read_json(LOG_FILE, default=[])


def _save_log(entries: list[dict]) -> int:
    return _write_json(LOG_FILE, entries)


@mcp.tool()
def manage_workout_log(action: str, entry: dict | None = None) -> dict:
    """CRUD over data/workout_log.json.

    Actions:
      - 'list'   : return all entries
      - 'read'   : return the entry whose id == entry['id']
      - 'create' : append entry; auto-fills id, date if missing
      - 'update' : replace fields on the entry whose id == entry['id']
      - 'delete' : remove the entry whose id == entry['id']

    A typical create entry::

        {"exercise": "Bench Press", "sets": 3, "reps": 8, "weight": 135,
         "rpe": 7, "muscle_group": "chest", "notes": "felt smooth"}
    """
    with log_call("manage_workout_log", action=action, entry=entry) as box:
        action = (action or "").lower()
        log_step("[step 1/3] reading data/workout_log.json ...")
        entries = _load_log()
        log_step(f"[step 1/3] {len(entries)} entries currently on disk")

        if action == "list":
            log_step("[step 2/3] returning full list (no mutation)")
            box["result"] = {"ok": True, "count": len(entries), "entries": entries}
            return box["result"]

        if action == "read":
            target_id = (entry or {}).get("id")
            log_step(f"[step 2/3] looking up id={target_id}")
            match = next((e for e in entries if e.get("id") == target_id), None)
            box["result"] = {"ok": match is not None, "entry": match}
            return box["result"]

        if action == "create":
            new_entry = dict(entry or {})
            new_entry.setdefault("id", uuid.uuid4().hex[:8])
            new_entry.setdefault("date", date.today().isoformat())
            new_entry.setdefault("logged_at", datetime.now().isoformat(timespec="seconds"))
            log_step(f"[step 2/3] appending new entry id={new_entry['id']}")
            entries.append(new_entry)
            n_bytes = _save_log(entries)
            log_step(f"[step 3/3] wrote {n_bytes} bytes ({len(entries)} entries)")
            box["result"] = {"ok": True, "id": new_entry["id"], "total": len(entries)}
            return box["result"]

        if action == "update":
            target_id = (entry or {}).get("id")
            log_step(f"[step 2/3] updating id={target_id}")
            updated = False
            for e in entries:
                if e.get("id") == target_id:
                    e.update({k: v for k, v in (entry or {}).items() if k != "id"})
                    updated = True
                    break
            if not updated:
                log_step(f"[step 3/3] id={target_id} not found — no write")
                box["result"] = {"ok": False, "reason": "not_found"}
                return box["result"]
            n_bytes = _save_log(entries)
            log_step(f"[step 3/3] wrote {n_bytes} bytes")
            box["result"] = {"ok": True, "id": target_id}
            return box["result"]

        if action == "delete":
            target_id = (entry or {}).get("id")
            log_step(f"[step 2/3] deleting id={target_id}")
            before = len(entries)
            entries = [e for e in entries if e.get("id") != target_id]
            if len(entries) == before:
                log_step(f"[step 3/3] id={target_id} not found — no write")
                box["result"] = {"ok": False, "reason": "not_found"}
                return box["result"]
            n_bytes = _save_log(entries)
            log_step(f"[step 3/3] wrote {n_bytes} bytes ({len(entries)} entries)")
            box["result"] = {"ok": True, "id": target_id, "remaining": len(entries)}
            return box["result"]

        raise ValueError(
            f"Unknown action {action!r}. Use list/read/create/update/delete."
        )


# ---------------------------------------------------------------------------
# Tool 3 — STATS: pure compute over the log file
# ---------------------------------------------------------------------------

def _safe_num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


@mcp.tool()
def get_stats(range: str = "all") -> dict:
    """Compute summary stats over the workout log.

    Args:
        range: 'week', 'month', or 'all'.

    Returns a dict with: streak_days, sessions, total_volume, weekly_volume,
    by_muscle_group, prs (heaviest set per exercise), avg_rpe.
    """
    with log_call("get_stats", range=range) as box:
        log_step("[step 1/4] reading log ...")
        entries = _load_log()
        log_step(f"[step 1/4] {len(entries)} entries")

        log_step(f"[step 2/4] applying range filter: {range}")
        today = date.today()
        if range == "week":
            cutoff = today - timedelta(days=7)
        elif range == "month":
            cutoff = today - timedelta(days=30)
        else:
            cutoff = date.min

        def in_range(e: dict) -> bool:
            try:
                return datetime.fromisoformat(e.get("date", "")).date() >= cutoff
            except Exception:
                return False

        filt = [e for e in entries if in_range(e)] if range != "all" else entries
        log_step(f"[step 2/4] {len(filt)} entries in range")

        log_step("[step 3/4] computing rollups ...")
        # Streak = consecutive days with >=1 entry, counting back from today.
        dates = sorted({e.get("date") for e in entries if e.get("date")}, reverse=True)
        streak = 0
        cursor = today
        for d in dates:
            try:
                d_parsed = datetime.fromisoformat(d).date()
            except Exception:
                continue
            if d_parsed == cursor:
                streak += 1
                cursor = cursor - timedelta(days=1)
            elif d_parsed < cursor:
                break

        # Volume = sets * reps * weight per entry, summed and bucketed.
        total_volume = 0.0
        by_group: dict[str, float] = {}
        weekly: dict[str, float] = {}
        prs: dict[str, dict] = {}
        rpes: list[float] = []

        for e in filt:
            sets = _safe_num(e.get("sets"))
            reps = _safe_num(e.get("reps"))
            weight = _safe_num(e.get("weight"))
            vol = sets * reps * weight
            total_volume += vol
            grp = (e.get("muscle_group") or "other").lower()
            by_group[grp] = by_group.get(grp, 0.0) + vol
            try:
                d = datetime.fromisoformat(e.get("date", "")).date()
                # ISO week label, e.g. 2026-W19
                yr, wk, _ = d.isocalendar()
                key = f"{yr}-W{wk:02d}"
                weekly[key] = weekly.get(key, 0.0) + vol
            except Exception:
                pass
            ex_name = e.get("exercise") or "?"
            cur = prs.get(ex_name)
            if cur is None or weight > cur["weight"]:
                prs[ex_name] = {"weight": weight, "reps": reps, "date": e.get("date")}
            rpe = e.get("rpe")
            if rpe is not None:
                rpes.append(_safe_num(rpe))

        avg_rpe = round(sum(rpes) / len(rpes), 2) if rpes else None
        weekly_sorted = dict(sorted(weekly.items()))

        log_step("[step 4/4] packaging result")
        result = {
            "range": range,
            "streak_days": streak,
            "sessions": len(filt),
            "total_volume": round(total_volume, 1),
            "weekly_volume": weekly_sorted,
            "by_muscle_group": {k: round(v, 1) for k, v in by_group.items()},
            "prs": prs,
            "avg_rpe": avg_rpe,
        }
        box["result"] = result
        return result


# ---------------------------------------------------------------------------
# Tool 4 — UI: render a Prefab dashboard.  This is the @mcp.tool(app=True)
# tool, so its return value is a PrefabApp the host renders inline.
# ---------------------------------------------------------------------------

# Prefab is imported lazily so that running just the CRUD tools (e.g. from
# the agent demo) doesn't require prefab-ui at import time.

@mcp.tool()
def render_planner() -> dict:
    """Build a dashboard for the current plan and log.

    Returns a JSON description of the dashboard and writes the full spec
    to `data/last_dashboard.json`.  Any front-end (CLI, web, the agent
    demo) can consume that file.

    A second, richer variant — `render_planner_ui` — is registered
    automatically when `prefab-ui` is installed.  It uses the same data
    but returns a real `PrefabApp` so a Prefab-aware MCP host
    (`fastmcp dev apps ...`) can render the dashboard inline.  See the
    `try: import prefab_ui ...` block near the bottom of this file.
    """
    with log_call("render_planner") as box:
        log_step("[step 1/3] loading plan + log ...")
        plan = _read_json(PLAN_FILE, default={})
        entries = _load_log()
        log_step(f"[step 1/3] plan_id={plan.get('plan_id')!r}, log entries={len(entries)}")

        log_step("[step 2/3] computing stats for the Stats tab ...")
        # Reuse the stats function but bypass the wrapping log_call so we
        # don't double-log; call the raw rollup inline instead.
        stats = get_stats.fn(range="all") if hasattr(get_stats, "fn") else None
        if stats is None:
            # FastMCP wraps tools; reach for the underlying function.
            stats = _compute_stats_inline(entries)
        log_step("[step 2/3] stats ready")

        log_step("[step 3/3] assembling dashboard spec ...")
        today_idx = date.today().weekday() % max(len(plan.get("days") or []), 1)
        today_day = (plan.get("days") or [{}])[today_idx] if plan.get("days") else {}

        spec = {
            "title": "Exercise Planner",
            "tabs": [
                {
                    "name": "Overview",
                    "rows": [
                        {"day": d.get("day"), "exercises": [e["name"] for e in d.get("exercises", [])]}
                        for d in plan.get("days", [])
                    ],
                },
                {
                    "name": "Today",
                    "day_name": today_day.get("day"),
                    "exercises": [
                        {
                            "name": e.get("name"),
                            "target": f"{e.get('target_sets')}×{e.get('target_reps')}",
                            "tag": e.get("tag"),
                            "image": e.get("image"),
                        }
                        for e in (today_day.get("exercises") or [])
                    ],
                },
                {
                    "name": "History",
                    "rows": [
                        {
                            "date": e.get("date"),
                            "exercise": e.get("exercise"),
                            "sets": e.get("sets"),
                            "reps": e.get("reps"),
                            "weight": e.get("weight"),
                            "rpe": e.get("rpe"),
                        }
                        for e in entries[-20:]
                    ],
                },
                {
                    "name": "Stats",
                    "streak_days": stats.get("streak_days"),
                    "sessions": stats.get("sessions"),
                    "total_volume": stats.get("total_volume"),
                    "by_muscle_group": stats.get("by_muscle_group"),
                    "prs": stats.get("prs"),
                    "avg_rpe": stats.get("avg_rpe"),
                },
            ],
        }
        log_step(
            f"[step 3/3] tabs={[t['name'] for t in spec['tabs']]} ; "
            f"today exercises={len(spec['tabs'][1]['exercises'])}"
        )
        box["result"] = {"rendered": True, "tabs": [t["name"] for t in spec["tabs"]]}
        # Stash the full spec on disk so the Prefab-aware variant (or a
        # browser front-end) can pick it up.
        _write_json(DATA_DIR / "last_dashboard.json", spec)
        return {"ok": True, "spec_path": str(DATA_DIR / "last_dashboard.json"), "summary": box["result"]}


def _compute_stats_inline(entries: list[dict]) -> dict:
    """Bypass-log version of get_stats for use inside other tools."""
    total_volume = 0.0
    by_group: dict[str, float] = {}
    prs: dict[str, dict] = {}
    rpes: list[float] = []
    for e in entries:
        sets = _safe_num(e.get("sets"))
        reps = _safe_num(e.get("reps"))
        weight = _safe_num(e.get("weight"))
        vol = sets * reps * weight
        total_volume += vol
        grp = (e.get("muscle_group") or "other").lower()
        by_group[grp] = by_group.get(grp, 0.0) + vol
        ex_name = e.get("exercise") or "?"
        cur = prs.get(ex_name)
        if cur is None or weight > cur["weight"]:
            prs[ex_name] = {"weight": weight, "reps": reps, "date": e.get("date")}
        if e.get("rpe") is not None:
            rpes.append(_safe_num(e.get("rpe")))
    today = date.today()
    dates = sorted({e.get("date") for e in entries if e.get("date")}, reverse=True)
    streak = 0
    cursor = today
    for d in dates:
        try:
            d_parsed = datetime.fromisoformat(d).date()
        except Exception:
            continue
        if d_parsed == cursor:
            streak += 1
            cursor = cursor - timedelta(days=1)
        elif d_parsed < cursor:
            break
    return {
        "streak_days": streak,
        "sessions": len(entries),
        "total_volume": round(total_volume, 1),
        "by_muscle_group": {k: round(v, 1) for k, v in by_group.items()},
        "prs": prs,
        "avg_rpe": round(sum(rpes) / len(rpes), 2) if rpes else None,
    }


# ---------------------------------------------------------------------------
# Optional: a Prefab UI tool.  Only registered if prefab-ui imports cleanly.
# ---------------------------------------------------------------------------

try:
    from prefab_ui.app import PrefabApp  # type: ignore
    from prefab_ui.components import (  # type: ignore
        Badge,
        Card,
        CardContent,
        CardHeader,
        CardTitle,
        Column,
        H1,
        H3,
        Muted,
        Progress,
        Row,
        Tab,
        Tabs,
        Text,
    )
    from prefab_ui.components.charts import BarChart, ChartSeries, PieChart  # type: ignore

    @mcp.tool(app=True)
    def render_planner_ui() -> "PrefabApp":  # type: ignore[name-defined]
        """Return a Prefab dashboard.  Use inside a Prefab-aware MCP host."""
        with log_call("render_planner_ui") as box:
            plan = _read_json(PLAN_FILE, default={})
            entries = _load_log()
            stats = _compute_stats_inline(entries)
            today_days = plan.get("days") or []
            today_day = today_days[date.today().weekday() % len(today_days)] if today_days else {}

            with PrefabApp(css_class="max-w-5xl mx-auto p-6") as app:
                with Card():
                    with CardHeader():
                        CardTitle("Exercise Planner")
                        Muted(
                            f"plan {plan.get('plan_id', '—')} · "
                            f"{plan.get('days_per_week', '?')}d/wk · "
                            f"goal: {plan.get('goal', '?')}"
                        )
                    with CardContent():
                        with Tabs(value="today"):
                            # --- Overview ---
                            with Tab("Overview", value="overview"):
                                with Column(gap=4):
                                    if not today_days:
                                        Muted("No plan yet — call fetch_exercise_plan first.")
                                    for d in today_days:
                                        with Card():
                                            with CardContent():
                                                with Column(gap=1):
                                                    H3(d.get("day", "Day"))
                                                    for ex in d.get("exercises", []):
                                                        Text(
                                                            f"• {ex.get('name')}  "
                                                            f"({ex.get('target_sets')}×{ex.get('target_reps')})"
                                                        )

                            # --- Today ---
                            with Tab("Today", value="today"):
                                with Column(gap=3):
                                    H3(today_day.get("day", "Today"))
                                    if not today_day.get("exercises"):
                                        Muted("Rest day or no plan loaded.")
                                    for ex in today_day.get("exercises", []):
                                        with Card():
                                            with CardContent():
                                                with Row(gap=4):
                                                    with Column(gap=1):
                                                        H3(ex.get("name", "Exercise"))
                                                        Muted(
                                                            f"{ex.get('tag', '')} · "
                                                            f"{ex.get('target_sets')}×{ex.get('target_reps')}"
                                                        )
                                                        if ex.get("image"):
                                                            Muted(f"GIF: {ex['image']}")

                            # --- History ---
                            with Tab("History", value="history"):
                                with Column(gap=2):
                                    H3("Recent sessions")
                                    if not entries:
                                        Muted("No entries logged yet.")
                                    for e in entries[-15:][::-1]:
                                        with Row(gap=3):
                                            Text(e.get("date", "—"))
                                            Text(e.get("exercise", "—"))
                                            Badge(
                                                f"{e.get('sets')}×{e.get('reps')} @ {e.get('weight')}",
                                                variant="default",
                                            )
                                            if e.get("rpe") is not None:
                                                Badge(f"RPE {e['rpe']}", variant="warning")

                            # --- Stats ---
                            with Tab("Stats", value="stats"):
                                with Column(gap=4):
                                    with Row(gap=4):
                                        with Column(gap=1):
                                            Muted("Streak")
                                            H1(f"{stats['streak_days']}d")
                                        with Column(gap=1):
                                            Muted("Sessions")
                                            H1(f"{stats['sessions']}")
                                        with Column(gap=1):
                                            Muted("Total volume")
                                            H1(f"{int(stats['total_volume'])}")
                                    if stats["by_muscle_group"]:
                                        H3("Volume by muscle group")
                                        PieChart(
                                            data=[
                                                {"name": k, "value": v}
                                                for k, v in stats["by_muscle_group"].items()
                                            ],
                                            data_key="value",
                                            name_key="name",
                                            show_legend=True,
                                        )
                                    if stats["prs"]:
                                        H3("Personal records")
                                        for ex_name, pr in stats["prs"].items():
                                            with Row(gap=3):
                                                Text(ex_name)
                                                Badge(
                                                    f"{pr['weight']} × {pr['reps']}",
                                                    variant="success",
                                                )
                                                Muted(pr.get("date") or "")
            box["result"] = {"rendered": True}
            return app

    _emit(f"[{_ts()}] [info] Prefab UI tool registered (render_planner_ui)")
except Exception as _prefab_err:  # noqa: BLE001
    _emit(
        f"[{_ts()}] [info] prefab-ui not available — UI tool skipped "
        f"({_prefab_err.__class__.__name__}: {_prefab_err})"
    )


# ===========================================================================

if __name__ == "__main__":
    SERVER_LOG.write_text("")  # truncate at server start for clean traces
    _emit(f"[{_ts()}] ===== server start =====")
    _emit(f"[{_ts()}] [info] data dir : {DATA_DIR}")
    _emit(f"[{_ts()}] [info] log  file : {SERVER_LOG}")
    _emit(
        f"[{_ts()}] [info] tools registered: "
        f"fetch_exercise_plan, manage_workout_log, get_stats, render_planner"
        + (", render_planner_ui" if "render_planner_ui" in globals() else "")
    )
    mcp.run()
