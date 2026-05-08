"""
live_demo.py — one command, one window, watch the agent populate a
Prefab dashboard live.

Flow:
  1. Writes `_dashboard.py` (the Prefab UI — reads data/*.json on every render).
  2. Spawns `prefab serve _dashboard.py --reload` as a subprocess.
  3. Opens http://127.0.0.1:5175 in your browser — you'll see an empty
     dashboard waiting for the agent.
  4. Asks YOU a few questions in the terminal (goal, days/week, equipment,
     and optionally a workout to log).
  5. Hands those answers to the Gemini agent loop.
  6. After every tool call, touches `_dashboard.py` so Prefab's --reload
     triggers a refresh in the browser.  You watch the dashboard fill in.
  7. On Ctrl+C (or after FINAL_ANSWER), kills the Prefab subprocess.

Run:
    python live_demo.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import webbrowser
from concurrent.futures import TimeoutError
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()

HERE = Path(__file__).parent
DATA_DIR = HERE / "data"
DASHBOARD_PY = HERE / "_dashboard.py"
PREFAB_LOG = HERE / "logs" / "prefab_server.log"
# Best-guess default; we discover the real URL by reading the Prefab log
# right after the subprocess starts.  If something else is already on
# :5175, Prefab quietly falls back to :5176, :5177, etc.
PREFAB_URL = "http://127.0.0.1:5175"

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
MAX_ITERATIONS = 8
LLM_SLEEP_SECONDS = 7
LLM_TIMEOUT = 30


API_KEY = os.getenv("GEMINI_API_KEY")
if not API_KEY:
    raise SystemExit("GEMINI_API_KEY missing in .env")
client = genai.Client(api_key=API_KEY)


# ---------------------------------------------------------------------------
# Interactive input collection
# ---------------------------------------------------------------------------

def _clean(s: str) -> str:
    """Strip whitespace and any [brackets] the user wrapped around their input."""
    s = s.strip()
    # Some users see "(default: X)" and type "[X]" or "(X)" — be lenient.
    if len(s) >= 2 and s[0] in "[(" and s[-1] in "])":
        s = s[1:-1].strip()
    return s


def _ask(prompt: str, default: str) -> str:
    """input() that accepts blank → default.  Default shown in parentheses."""
    raw = input(f"{prompt} (default: {default}) > ")
    return _clean(raw) or default


def _ask_int(prompt: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    """Loop until the user gives a parseable int (optionally in [lo, hi])."""
    while True:
        raw = _ask(prompt, str(default))
        try:
            v = int(raw)
        except ValueError:
            print(f"  '{raw}' isn't a number — try again.")
            continue
        if lo is not None and v < lo:
            print(f"  must be ≥ {lo} — try again.")
            continue
        if hi is not None and v > hi:
            print(f"  must be ≤ {hi} — try again.")
            continue
        return v


def _ask_float(prompt: str, default: float) -> float:
    while True:
        raw = _ask(prompt, str(default))
        try:
            return float(raw)
        except ValueError:
            print(f"  '{raw}' isn't a number — try again.")


def collect_inputs() -> dict:
    """Walk the user through the prompts.  Returns a normalised dict."""
    print()
    print("-" * 60)
    print("Tell me about the workout plan you'd like.")
    print("Just type your answer and press Enter — leave blank to accept the default.")
    print("-" * 60)

    goal = _ask("Goal — strength / hypertrophy / endurance", "hypertrophy").lower()
    if goal not in {"strength", "hypertrophy", "endurance"}:
        print(f"  (unknown goal {goal!r}, falling back to 'hypertrophy')")
        goal = "hypertrophy"

    days = _ask_int("Days per week", 4, lo=3, hi=6)

    eq_raw = _ask("Equipment (comma-separated)", "Dumbbell, Barbell")
    equipment = [s.strip() for s in eq_raw.split(",") if s.strip()]

    log_raw = _ask("Log a session today? (y/n)", "y").lower()
    session: dict | None = None
    if log_raw.startswith("y"):
        print("  Tell me about the session:")
        exercise = _ask("    Exercise", "Bench Press")
        sets = _ask_int("    Sets", 3, lo=1, hi=20)
        reps = _ask_int("    Reps", 8, lo=1, hi=100)
        weight = _ask_float("    Weight (lb)", 135.0)
        rpe = _ask_int("    RPE (1-10)", 7, lo=1, hi=10)
        muscle = _ask("    Muscle group", "chest").lower()
        session = {
            "exercise": exercise,
            "sets": sets,
            "reps": reps,
            "weight": weight,
            "rpe": rpe,
            "muscle_group": muscle,
        }

    return {
        "goal": goal,
        "days_per_week": days,
        "equipment": equipment,
        "session": session,
    }


def build_task(inp: dict) -> str:
    """Render the user's answers into the natural-language task string."""
    eq_str = ", ".join(inp["equipment"]) if inp["equipment"] else "any equipment"
    parts = [
        f"I want a {inp['days_per_week']}-day {inp['goal']} plan that uses {eq_str}."
    ]
    if inp["session"]:
        s = inp["session"]
        parts.append(
            f"After the plan is built, log today's workout: "
            f"{s['exercise']}, {s['sets']} sets x {s['reps']} reps at "
            f"{s['weight']} lb, RPE {s['rpe']}, muscle_group={s['muscle_group']}."
        )
    parts.extend(
        [
            "Then call get_stats with range='all' so I can see my volume.",
            "Finally call render_planner so the dashboard is generated.",
            "When everything is done, give a FINAL_ANSWER summarising what you did.",
        ]
    )
    return " ".join(parts)


# ---------------------------------------------------------------------------
# The Prefab dashboard.  We REWRITE this file with embedded data on every
# tool call.  Because the file content actually changes, prefab serve's
# --reload watcher fires and the connected browser refreshes.  This avoids
# restarting the subprocess (which would cause TIME_WAIT port-binding
# failures and "127.0.0.1 refused to connect" errors).
# ---------------------------------------------------------------------------

# Counter incremented each rebuild — its only job is to guarantee that the
# generated source differs from the previous version, so the file watcher
# never thinks "nothing changed, no reload."
_REBUILD_COUNTER = {"n": 0}


def _stats_for(entries: list[dict]) -> dict:
    total_volume = 0.0
    by_group: dict[str, float] = {}
    prs: dict[str, dict] = {}
    for e in entries:
        try:
            sets = float(e.get("sets", 0))
            reps = float(e.get("reps", 0))
            weight = float(e.get("weight", 0))
        except Exception:
            sets = reps = weight = 0.0
        v = sets * reps * weight
        total_volume += v
        g = (e.get("muscle_group") or "other").lower()
        by_group[g] = by_group.get(g, 0.0) + v
        nm = e.get("exercise") or "?"
        cur = prs.get(nm)
        if cur is None or weight > cur["weight"]:
            prs[nm] = {"weight": weight, "reps": reps, "date": e.get("date")}
    return {
        "total_volume": round(total_volume, 1),
        "sessions": len(entries),
        "by_muscle_group": {k: round(v, 1) for k, v in by_group.items()},
        "prs": prs,
    }


def build_dashboard_source(plan: dict, entries: list[dict]) -> str:
    """Render the full _dashboard.py source with `plan` + `entries` baked in.

    All data is embedded as Python literals via repr(), so the dashboard
    has no I/O at render time — its content is fully determined by what's
    in this file.  That makes prefab's --reload reliable: every rewrite
    produces a textually different file.
    """
    from datetime import date as _date

    _REBUILD_COUNTER["n"] += 1
    rebuild_id = _REBUILD_COUNTER["n"]

    stats = _stats_for(entries)
    days = plan.get("days") or []
    today_day = days[_date.today().weekday() % len(days)] if days else {}

    plan_repr = repr(plan)
    today_day_repr = repr(today_day)
    entries_repr = repr(entries[-15:][::-1])  # last 15, newest-first
    stats_repr = repr(stats)

    return f'''"""Auto-generated by live_demo.py — rewritten on every tool call."""
# rebuild #{rebuild_id} — content differs from the previous version so that
# prefab serve --reload picks it up and the browser refreshes automatically.

from prefab_ui.app import PrefabApp
from prefab_ui.components import (
    Badge, Card, CardContent, CardHeader, CardTitle,
    Column, H1, H3, Muted, Row, Tab, Tabs, Text,
)
try:
    from prefab_ui.components.charts import PieChart  # type: ignore
    HAS_CHARTS = True
except Exception:
    HAS_CHARTS = False

# --- baked-in state ---
plan = {plan_repr}
today_day = {today_day_repr}
entries = {entries_repr}
stats = {stats_repr}


with PrefabApp(css_class="max-w-5xl mx-auto p-6") as app:
    with Card():
        with CardHeader():
            CardTitle("Exercise Planner — live")
            if plan:
                Muted(
                    f"plan {{plan.get('plan_id', '—')}} · "
                    f"{{plan.get('days_per_week', '?')}}d/wk · "
                    f"goal: {{plan.get('goal', '?')}}"
                )
            else:
                Muted("Waiting for the agent to call fetch_exercise_plan ...")
        with CardContent():
            with Tabs(value="today"):
                with Tab("Overview", value="overview"):
                    with Column(gap=4):
                        if not (plan.get("days") or []):
                            Muted("(no plan yet)")
                        for d in (plan.get("days") or []):
                            with Card():
                                with CardContent():
                                    with Column(gap=1):
                                        H3(d.get("day", "Day"))
                                        for ex in d.get("exercises", []):
                                            Text(
                                                f"• {{ex.get('name')}}  "
                                                f"({{ex.get('target_sets')}}×{{ex.get('target_reps')}})"
                                            )

                with Tab("Today", value="today"):
                    with Column(gap=3):
                        H3(today_day.get("day", "Today") if today_day else "Today")
                        if not today_day.get("exercises"):
                            Muted("(rest day or no plan loaded)")
                        for ex in today_day.get("exercises", []):
                            with Card():
                                with CardContent():
                                    with Column(gap=1):
                                        H3(ex.get("name", "Exercise"))
                                        Muted(
                                            f"{{ex.get('tag', '')}} · "
                                            f"{{ex.get('target_sets')}}×{{ex.get('target_reps')}}"
                                        )
                                        if ex.get("image"):
                                            Muted(f"GIF: {{ex['image']}}")

                with Tab("History", value="history"):
                    with Column(gap=2):
                        H3("Recent sessions")
                        if not entries:
                            Muted("(no entries yet)")
                        for e in entries:
                            with Row(gap=3):
                                Text(e.get("date", "—"))
                                Text(e.get("exercise", "—"))
                                Badge(
                                    f"{{e.get('sets')}}×{{e.get('reps')}} @ {{e.get('weight')}}",
                                    variant="default",
                                )
                                if e.get("rpe") is not None:
                                    Badge(f"RPE {{e['rpe']}}", variant="warning")

                with Tab("Stats", value="stats"):
                    with Column(gap=4):
                        with Row(gap=4):
                            with Column(gap=1):
                                Muted("Sessions"); H1(str(stats["sessions"]))
                            with Column(gap=1):
                                Muted("Total volume"); H1(str(int(stats["total_volume"])))
                        if HAS_CHARTS and stats["by_muscle_group"]:
                            H3("Volume by muscle group")
                            PieChart(
                                data=[
                                    {{"name": k, "value": v}}
                                    for k, v in stats["by_muscle_group"].items()
                                ],
                                data_key="value",
                                name_key="name",
                                show_legend=True,
                            )
                        if stats["prs"]:
                            H3("Personal records")
                            for nm, pr in stats["prs"].items():
                                with Row(gap=3):
                                    Text(nm)
                                    Badge(
                                        f"{{pr['weight']}} × {{pr['reps']}}",
                                        variant="success",
                                    )
                                    Muted(pr.get("date") or "")
'''


def _read_disk_state() -> tuple[dict, list[dict]]:
    """Pull the latest plan + log off disk."""
    plan_path = DATA_DIR / "current_plan.json"
    log_path = DATA_DIR / "workout_log.json"
    plan: dict = {}
    entries: list[dict] = []
    if plan_path.exists():
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except Exception:
            plan = {}
    if log_path.exists():
        try:
            entries = json.loads(log_path.read_text(encoding="utf-8"))
        except Exception:
            entries = []
    return plan, entries


def write_dashboard():
    """(Re)build _dashboard.py with the current on-disk state baked in."""
    plan, entries = _read_disk_state()
    DASHBOARD_PY.write_text(
        build_dashboard_source(plan, entries), encoding="utf-8"
    )


def _detect_prefab_url(log_path: Path, timeout: float = 6.0) -> str | None:
    """Tail the Prefab log until we see a 'Serving at http://...' line.

    Prefab falls back to the next available port if 5175 is busy, so we
    can't trust the default.  This reads the real URL out of the log.
    """
    import re
    pattern = re.compile(r"Serving at (https?://\S+)")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if log_path.exists():
            content = log_path.read_text(encoding="utf-8", errors="replace")
            matches = pattern.findall(content)
            if matches:
                return matches[-1].rstrip("/")
        time.sleep(0.2)
    return None


def _force_browser_reload() -> None:
    """Tell macOS Chrome to reload the dashboard tab.  No-op on other OSes."""
    if sys.platform != "darwin":
        return
    # Build the AppleScript at call time so it uses the *current* PREFAB_URL,
    # which may have been replaced after we read it from the Prefab log.
    script = f'''
tell application "Google Chrome"
    set targetURL to "{PREFAB_URL}"
    repeat with w in windows
        repeat with t in tabs of w
            if URL of t starts with targetURL then
                reload t
            end if
        end repeat
    end repeat
end tell
'''
    try:
        subprocess.run(
            ["osascript", "-e", script], capture_output=True, timeout=3,
        )
    except Exception:
        pass


def refresh_dashboard():
    """Rewrite _dashboard.py with the latest on-disk state, then nudge the
    browser to reload.

    `prefab serve --reload` watches the .py file, but its WebSocket push
    to the browser is unreliable in our setup.  After rewriting the file
    we ask Chrome (via AppleScript on macOS) to reload the dashboard tab
    so the user sees the change without manually refreshing.
    """
    write_dashboard()
    _force_browser_reload()


# ---------------------------------------------------------------------------
# Prefab subprocess management
# ---------------------------------------------------------------------------

class PrefabServer:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.log = None

    def start(self):
        PREFAB_LOG.parent.mkdir(parents=True, exist_ok=True)
        self.log = open(PREFAB_LOG, "a")
        self.log.write("\n===== prefab start =====\n")
        self.log.flush()
        self.proc = subprocess.Popen(
            ["prefab", "serve", str(DASHBOARD_PY), "--reload"],
            cwd=HERE,
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )

    def stop(self):
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
            except Exception:
                pass
            self.proc = None
        if self.log is not None:
            try:
                self.log.close()
            except Exception:
                pass
            self.log = None


# ---------------------------------------------------------------------------
# Agent helpers
# ---------------------------------------------------------------------------

async def generate_with_timeout(prompt: str, timeout: int = LLM_TIMEOUT):
    loop = asyncio.get_event_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(
            None, lambda: client.models.generate_content(model=MODEL, contents=prompt)
        ),
        timeout=timeout,
    )


def describe_tools(tools) -> str:
    out = []
    for i, t in enumerate(tools, 1):
        props = (t.inputSchema or {}).get("properties", {})
        params = ", ".join(f"{n}: {p.get('type', '?')}" for n, p in props.items()) or "no params"
        out.append(f"{i}. {t.name}({params}) — {t.description or ''}")
    return "\n".join(out)


def _resolve_type(info: dict) -> str:
    t = info.get("type")
    if isinstance(t, str):
        return t
    if isinstance(t, list):
        for c in t:
            if c and c != "null":
                return c
    for opt in info.get("anyOf", []) or []:
        ct = opt.get("type")
        if ct and ct != "null":
            return ct
    return "string"


def coerce(value: str, info: dict):
    if value == "":
        return value
    s = value.strip()
    if s.startswith(("[", "{")):
        try:
            return json.loads(s)
        except Exception:
            pass
    schema_type = _resolve_type(info)
    if schema_type == "integer":
        return int(value)
    if schema_type == "number":
        return float(value)
    if schema_type in ("array", "object"):
        try:
            return json.loads(value)
        except Exception:
            return eval(value)
    if schema_type == "boolean":
        return value.lower() in ("true", "1", "yes")
    return value


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run_agent(task: str):
    server_params = StdioServerParameters(
        command="python", args=["exercise_planner_server.py"]
    )
    print("\nConnecting to exercise_planner_server (stdio) ...")
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            tools_desc = describe_tools(tools)
            print(f"Loaded {len(tools)} tools.\n")

            sys_prompt = f"""You are the user's personal exercise-planner agent.
You solve tasks by calling tools ONE AT A TIME and observing their results.

Available tools:
{tools_desc}

Respond with EXACTLY ONE line, in one of these two formats:
  FUNCTION_CALL: tool_name|arg1|arg2|...
  FINAL_ANSWER: <short natural-language summary of what you did>

Argument rules:
- Provide args in the EXACT ORDER of the tool's parameters.
- For list arguments use a JSON array, e.g. ["Dumbbell","Barbell"].
- For object arguments use a JSON object.
- An empty argument is a single empty string between two pipes (||).
"""

            history: list[str] = []
            for it in range(1, MAX_ITERATIONS + 1):
                print(f"\n--- Iteration {it} ---")
                ctx = "\n".join(history) if history else "(no prior steps)"
                prompt = (
                    f"{sys_prompt}\nTask: {task}\n\nPrevious steps:\n{ctx}\n\n"
                    f"What is your next single action?"
                )
                if LLM_SLEEP_SECONDS:
                    print(f"Sleeping {LLM_SLEEP_SECONDS}s before LLM call...")
                    await asyncio.sleep(LLM_SLEEP_SECONDS)

                try:
                    response = await generate_with_timeout(prompt)
                except (TimeoutError, asyncio.TimeoutError):
                    print("LLM timed out — stopping.")
                    return
                except Exception as e:
                    print(f"LLM error: {e}")
                    return

                lines = [l.strip() for l in (response.text or "").splitlines() if l.strip()]
                text = lines[0] if lines else ""
                print(f"LLM: {text}")

                if text.startswith("FINAL_ANSWER:"):
                    print("\n=== Agent done ===")
                    print(text)
                    return
                if not text.startswith("FUNCTION_CALL:"):
                    print(f"Unexpected reply: {response.text!r}")
                    return

                _, call = text.split(":", 1)
                parts = [p.strip() for p in call.split("|")]
                fn, raw_args = parts[0], parts[1:]
                tool = next((t for t in tools if t.name == fn), None)
                if tool is None:
                    history.append(f"Iteration {it}: unknown tool {fn!r}")
                    continue
                props = (tool.inputSchema or {}).get("properties", {})
                args = {n: coerce(v, info) for (n, info), v in zip(props.items(), raw_args)}

                print(f"→ {fn}({args})")
                try:
                    result = await session.call_tool(fn, arguments=args)
                    payload = (
                        result.content[0].text
                        if result.content and hasattr(result.content[0], "text")
                        else str(result)
                    )
                except Exception as e:
                    payload = f"ERROR: {e}"
                print(f"← {payload}")

                # >>>> the live-update bit <<<<
                # Restart the Prefab subprocess so the browser reloads with
                # the just-written data.  Disconnect lasts ~1s — visible blink.
                print("  (refreshing dashboard ...)")
                refresh_dashboard()

                short = payload if len(payload) <= 600 else payload[:600] + "...[truncated]"
                history.append(f"Iteration {it}: called {fn}({args}) → {short}")
            else:
                print("\nReached MAX_ITERATIONS without FINAL_ANSWER.")


def _reset_state() -> None:
    """Wipe any prior plan/log so the dashboard truly starts empty."""
    for name in ("current_plan.json", "workout_log.json", "last_dashboard.json"):
        p = DATA_DIR / name
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description="Exercise Planner live demo")
    parser.add_argument(
        "--keep-data",
        action="store_true",
        help="Don't wipe data/*.json at startup (default: wipe so the dashboard starts empty).",
    )
    args = parser.parse_args()

    print("=" * 72)
    print("Exercise Planner — LIVE demo")
    print("=" * 72)

    DATA_DIR.mkdir(exist_ok=True)
    if not args.keep_data:
        _reset_state()
    write_dashboard()

    # 1. Bring the dashboard up FIRST so the user sees an empty state
    #    before being asked anything.  Prefab serve auto-opens the browser
    #    on first run, so we don't call webbrowser.open() ourselves.
    global PREFAB_URL
    # Truncate the prefab log so _detect_prefab_url only sees this run's lines.
    PREFAB_LOG.parent.mkdir(parents=True, exist_ok=True)
    PREFAB_LOG.write_text("")

    server = PrefabServer()
    print(f"Starting Prefab dev server (logs → {PREFAB_LOG.name}) ...")
    server.start()

    detected = _detect_prefab_url(PREFAB_LOG)
    if detected:
        PREFAB_URL = detected
        if PREFAB_URL != "http://127.0.0.1:5175":
            print(f"  (port 5175 was busy; Prefab is on {PREFAB_URL} instead)")
    else:
        print(f"  (couldn't read Prefab log — assuming default {PREFAB_URL})")

    print(f"Dashboard at {PREFAB_URL} — Prefab should have opened it for you.")
    print("(If it didn't, paste that URL into your browser.)")
    time.sleep(1.0)

    try:
        # 2. Ask the user what they want.
        inputs = collect_inputs()
        task = build_task(inputs)
        if LLM_SLEEP_SECONDS:
            print(f"\n(LLM_SLEEP_SECONDS={LLM_SLEEP_SECONDS}; lower in source if too slow.)")

        print("\nTask the agent will execute:\n  " + task + "\n")

        # 3. Run the agent loop — each tool call refreshes the dashboard.
        asyncio.run(run_agent(task))

        # 4. Per-tool reloads are best-effort (AppleScript-driven on macOS).
        #    To guarantee the user sees the final populated state, open a
        #    fresh tab at the URL exactly once now.
        print("\nOpening a fresh tab with the final dashboard state ...")
        try:
            webbrowser.open(PREFAB_URL)
        except Exception:
            pass

        print("Agent finished. Dashboard is still up — Ctrl+C here to exit.")
        signal.pause() if hasattr(signal, "pause") else time.sleep(10**9)
    except KeyboardInterrupt:
        pass
    finally:
        print("\nShutting down Prefab server ...")
        server.stop()


if __name__ == "__main__":
    main()
