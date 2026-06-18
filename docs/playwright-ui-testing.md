# End-to-end UI testing with Playwright (didactic)

Personal reference for how I test browser-served apps (Streamlit, Flask, etc.)
in this stack. Captures the *mental model*, the *setup*, and the *reasoning*
behind the choices — not just the commands. If I'm bootstrapping on a fresh
PC, this is the doc I read.

> **Audience.** Me, plus any AI coding agent I hand a project to.
> **Status.** Living reference, not a changelog. Update in place when the
> recipe changes.

---

## TL;DR

- I run **two distinct testing loops**, on purpose. Don't conflate them.
- **Loop 1 = headed, agent-driven, throwaway.** During development, an AI
  agent (Claude Code, Codex CLI, etc.) drives a real Chromium window via
  the Playwright MCP server in `--headed` mode. I watch on screen. Nothing
  is committed.
- **Loop 2 = headless, pytest-playwright, permanent.** Optional. Lives at
  `tests/e2e/`. No LLM in the loop, runs in CI or pre-push, costs $0 per
  run. Only added when a behavior is worth pinning down forever.
- **Why split?** Different audiences (me-now vs future-me), different cost
  models, different lifespans. Mixing them creates either bloated test
  suites or expensive throwaway "tests."

---

## The mental model

Browser testing usually fails one of two ways:

1. **Manual click-through forever** — works, but I burn 10 min after every
   change. Doesn't scale; I skip it and ship bugs.
2. **Big test framework upfront** — invest two days writing Page Objects
   and fixtures, end up with a fragile suite that breaks on every CSS
   tweak. Fix-the-tests becomes its own job.

The fix is recognising that **"I want to verify this change"** and
**"I want to prevent this regression forever"** are different problems,
and they deserve different machinery.

| Aspect       | Loop 1 — Verification              | Loop 2 — Regression                 |
| ------------ | ---------------------------------- | ----------------------------------- |
| Audience     | Me, right now                       | Future-me, six months later         |
| Lifespan     | The conversation, then gone        | Permanent, in `tests/e2e/`          |
| Mode         | Headed (`--headed`), I watch       | Headless, runs in CI                |
| Driver       | AI agent via Playwright MCP        | `pytest-playwright`, no AI          |
| Cost / run   | ~$0.05–$0.25 (model tokens)        | $0 (no model)                       |
| Cadence      | Every change                       | On push / in CI                     |
| Failure mode | Agent loops if I don't cap actions | Suite rots if I don't trim it       |

If a verification flushes a bug *that would silently come back later*,
then — and only then — promote it to Loop 2.

---

## Loop 1 — Headed agent verification (the workhorse)

This is the loop I use 95% of the time. The agent boots the app, opens a
real browser, clicks around, and reports what it saw. I watch the window
because (a) it's reassuring, (b) I catch visual issues the agent's DOM
snapshot would miss, (c) it makes the agent's behavior auditable.

### Why "headed" matters

Headless mode is faster and cheaper, but I can't see what's happening, so:

- I can't tell *why* the agent thinks the page looks wrong.
- I miss visual regressions (alignment, contrast, accidental empty space).
- If the agent gets stuck in a loop, I notice slower.

For *verification*, "I see it work" is worth the small overhead of a
visible Chromium window. For *regression*, no human is watching, so
headless is correct.

### How the agent drives the browser

The cleanest mechanism is the **Playwright MCP server**
(`@playwright/mcp`). MCP (Model Context Protocol) is Anthropic's standard
for exposing tools to AI agents; the Playwright MCP exposes browser
actions (`browser_navigate`, `browser_click`, `browser_snapshot`, etc.)
as callable tools. The agent sees these alongside its built-in tools and
chooses when to use them.

**Key cost-efficiency design choice:** the MCP server returns an
**accessibility tree snapshot** by default, not a screenshot. The a11y
tree is structured DOM text — usually 500–2 000 tokens for a Streamlit
tab. A full-page screenshot, by contrast, can be 5 000–20 000 vision
tokens. **Snapshot first; screenshot only on failure or as final visual
confirmation.**

### Setup per tool

#### Claude Code

```powershell
# Project-scoped (writes .mcp.json in the repo, shareable via git)
claude mcp add --scope project playwright -- npx -y "@playwright/mcp@latest" --headed

# Or user-scoped (just for me, across all projects)
claude mcp add --scope user playwright -- npx -y "@playwright/mcp@latest" --headed
```

First run will download the Playwright MCP package and prompt to install
Chromium (`npx playwright install chromium`). Cached after that.

Verify:

```powershell
claude mcp list
```

Should show `playwright` connected.

#### Codex CLI

Same MCP server, configured via `~/.codex/config.toml`:

```toml
[mcp_servers.playwright]
command = "npx"
args = ["-y", "@playwright/mcp@latest", "--headed"]
```

#### GitHub Copilot CLI

No MCP support at the time of writing. Fallback: the agent runs a small
Python script via shell. Example one-shot the agent can write inline:

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    page = browser.new_page()
    page.goto("http://localhost:8501")
    page.get_by_role("tab", name="Classify").click()
    print(page.get_by_role("table").inner_text())
    browser.close()
```

Less ergonomic than MCP (the agent has to write/read whole scripts) but
functionally equivalent.

### Using it day-to-day

Typical session:

1. Make a code change.
2. Make sure Streamlit is running on `localhost:8501` (boot once, leave it).
3. Ask the agent something like:
   > *"Verify the Local classifier returns rows for the synthetic golden
   > set. Cap at 5 actions, report pass/fail, no screenshot unless failed."*
4. Watch the Chromium window pop up. Agent navigates, clicks, snapshots,
   reports.
5. If it fails, agent screenshots, I look at it, we iterate.

### Rules I follow (and ask agents to follow)

- **Boot the app once.** Don't restart Streamlit between iterations
  unless `set_page_config` or top-level imports changed. Restart cost is
  3–8 seconds × N iterations = real wall-clock waste.
- **Stable widget keys are the contract.** Always set `key=` on widgets
  I plan to assert against. The agent targets them via
  `page.get_by_role(..., name=...)` or `page.get_by_test_id(...)`. A
  rename is a test-breaking change, treat it like renaming a public
  function.
- **Cap actions per cycle.** Tell the agent ≤ 5 actions, then report.
  Stops runaway loops cold. Cheap insurance.
- **Snapshot, don't screenshot.** Reserve screenshots for failure and
  final confirmation.
- **Scope to the change.** "Test the app" → bad. "Verify the Classify
  tab returns rows after upload" → good.
- **Throwaway means throwaway.** Do NOT create `tests/e2e/` files
  during verification. If the verification deserves to live forever,
  that's a deliberate, separate decision (Loop 2).

---

## Loop 2 — Headless regression suite (optional)

Don't create this until I have evidence I need it. The signal is usually
"I just re-introduced the same UI bug for the second time." Until then,
it's premature.

### When a verification graduates to a regression test

All three must be true:

1. **Silent breakage would hurt.** Classifier returning wrong rows = yes.
   Tooltip rendering one pixel off = no.
2. **No unit test under `tests/` can catch it.** If a function-level
   test would suffice, write that instead — it's faster, cheaper, more
   focused.
3. **The behavior has stabilized.** I'm not still iterating on it.
   Pinning behavior in flux means rewriting the test on every change.

### Skeleton (only when I actually need it)

```
tests/
  e2e/
    conftest.py          # streamlit_app + page session fixtures
    test_classify.py     # one test per stable behavior
```

`conftest.py` boots the app once per pytest session:

```python
import subprocess
import time
import socket

import pytest
from playwright.sync_api import sync_playwright


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


@pytest.fixture(scope="session")
def streamlit_app():
    proc = subprocess.Popen(
        [".venv/Scripts/python.exe", "-m", "streamlit", "run", "app/app.py",
         "--server.headless", "true", "--server.port", "8501"],
    )
    for _ in range(60):
        if _port_open(8501):
            break
        time.sleep(0.5)
    yield "http://localhost:8501"
    proc.terminate()


@pytest.fixture(scope="session")
def page(streamlit_app):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(streamlit_app)
        yield page
        browser.close()
```

A test:

```python
def test_classify_local_returns_rows(page):
    page.get_by_role("tab", name="Classify").click()
    page.get_by_role("button", name="Run local classifier").click()
    page.wait_for_selector('[data-testid="stTable"]')
    assert page.get_by_role("table").locator("tbody tr").count() > 0
```

### Default timeout — make hangs self-name

Playwright's implicit action + navigation timeout is **30 s**. An auto-waiting call — `.click()`, `page.goto()`, `wait_for_selector()` with no explicit `timeout=` — that never settles blocks silently for the full 30 s, and a few stacking inside one test push the run into a multi-minute hang. Under `pytest-timeout`, the test finally aborts as a **black box that never names which wait hung** — forcing replay or log archaeology on a flake that may not recur.

Set an **explicit, bounded, env-tunable default** in conftest instead of inheriting Playwright's implicit 30 s. **15 s is the recommended base** — well under any `pytest-timeout`, generous for a real localhost action, short enough that a stuck wait fails immediately with `TimeoutError: ... waiting for <locator>` and names the hung call. Set it once in an `autouse` fixture so every test inherits it without per-call boilerplate.

Where to set it depends on the fixture shape in conftest:

**Tests using the pytest-playwright `page` fixture directly** — apply it on the page:

```python
import os

_DEFAULT_TIMEOUT_MS = int(os.environ.get("E2E_DEFAULT_TIMEOUT_MS", "15000"))

@pytest.fixture(autouse=True)
def _bound_default_timeouts(page) -> None:
    page.set_default_timeout(_DEFAULT_TIMEOUT_MS)
    page.set_default_navigation_timeout(_DEFAULT_TIMEOUT_MS)
```

**A custom fixture that does `context.new_page()`** (e.g. an `authed_page`) — apply it on the context instead; the default is consulted at action time, so a context-level cap covers every page created from it:

```python
@pytest.fixture(autouse=True)
def _bound_default_timeouts(context) -> None:
    context.set_default_timeout(_DEFAULT_TIMEOUT_MS)
    context.set_default_navigation_timeout(_DEFAULT_TIMEOUT_MS)
```

`expect()` web-first assertions carry their own 5 s default — leave them alone. An explicit `timeout=` on any single call still overrides. Widen for a slow hosted CI runner without a code change: set `E2E_DEFAULT_TIMEOUT_MS=20000` in the workflow env.

**Origin:** app-launcher#186 (a ~120 s CI e2e hang, opaque, never named) surfaced the cost of the implicit 30 s default; the convention landed in app-launcher PR #189. whatsapp-radar already sets an explicit `page.set_default_timeout` with a 30 s × scale-factor variant (`_scaled_page_timeouts`) — satisfies the spirit. The **15 s bounded base is the fleet standard** across all sister repos.

### Running it

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/e2e/
```

POSIX:

```bash
./.venv/bin/python -m pytest tests/e2e/
```

### Rules

- **Keep it small.** Target < 15 tests total. If I'm tempted to add
  #20, delete two first.
- **No Page Object Model.** Too much ceremony at this scale.
- **One shared fixture.** Don't build a framework until I have three
  tests that need the same helper.
- **Boot-or-adopt, fail loud.** The session fixture should boot the app
  — and any service dependencies (a separate API process, a worker, a
  PTY host) — on a free/fixed port, or adopt one already listening, and
  **hard-fail (never `pytest.skip`)** if it can't. A suite that skips on
  a missing server reports green on a build it never tested. The
  `streamlit_app` skeleton above is the single-process Streamlit
  instance of this pattern; `uvicorn` / `flask run` are the same shape,
  and a multi-process app boots each dependency the same way.
- **Don't gate commits on it.** Run on push or in CI, never in
  pre-commit. Slow pre-commit hooks train me to bypass them.
- **Delete with the feature.** When I remove a feature, remove its
  e2e test in the same commit.

### Mobile projection (phone-first apps only)

If the app's primary surface is a phone, run the regression suite on
**WebKit** with a Playwright device descriptor (iPhone / Android —
viewport, user-agent, touch, scale). WebKit shares the iOS Safari
rendering + JS engine, so it catches most engine-specific mobile bugs on
a normal Windows/Linux box. Make the projection always-on via a
parametrised fixture so it can't be forgotten.

WebKit-on-Windows is not real iOS (no iOS shell, no real WKWebView
limits, no Apple keyboard). For the residual shell-only bugs, attach PC
DevTools to a real phone with `ios-webkit-debug-proxy`. Playwright
cannot drive real iOS Safari — only its bundled WebKit and the macOS
iOS Simulator.

---

## Cost economics

### Loop 1 (agent, headed)

A typical verification cycle on a Streamlit tab:

| Step                | Approx tokens         |
| ------------------- | --------------------- |
| `browser_navigate`  | ~300 in / 100 out     |
| `browser_snapshot`  | ~1 000–2 000 in       |
| `browser_click`     | ~200 in / 100 out     |
| Agent reasoning     | ~1 000–3 000 in/out   |
| **Total / cycle**   | **~5 000–10 000 in**  |

At current rates:

| Model       | Per cycle    | A 10-cycle dev session |
| ----------- | ------------ | ---------------------- |
| Sonnet 4.6  | ~$0.02–0.05  | ~$0.20–0.50            |
| Opus 4.7    | ~$0.10–0.25  | ~$1–2.50               |

A *failed* cycle that triggers a screenshot adds ~$0.05–0.20 in vision
tokens. Worth it on failure (the picture diagnoses the problem); not
worth it as a default.

### Loop 2 (regression, headless)

**$0 per run.** No model in the loop. Cost is wall-clock: ~2–5 s per
test on a warm boot, ~10 s for the cold boot. A 10-test suite runs in
under a minute. Free to run on every push.

### The expensive failure mode

The thing that actually wastes money is an agent that **loops** because
the page state confused it (Streamlit rerun timing, websocket reconnect,
modal it didn't expect). Mitigation:

- Cap actions per cycle in the prompt.
- Tell the agent: "if state is unexpected, stop and ask, do not
  guess." This is cheaper than 30 speculative tool calls.

---

## Best practices, in priority order

1. **Stable `key=` on every widget you'll assert against.** Rename =
   breaking change.
2. **Test behaviors, not rendering.** "Upload + Classify produces
   non-empty table" = behavior. "Button exists" = noise.
3. **Boot once, navigate often.** Restart only when the app's startup
   code changed.
4. **a11y snapshot beats screenshot, almost always.**
5. **Cap iterations.** ≤ 5 actions per verification cycle.
6. **Don't write `tests/e2e/` for verification.** Throwaway is the
   default; permanence is the exception.
7. **Pin Playwright + browser version** if you commit a regression
   suite. `requirements.txt` plus `playwright install chromium` in the
   bootstrap.
8. **Trim aggressively.** A growing e2e suite that nobody trims is
   the actual bloat. Delete the test when you delete the feature.

---

## Bootstrap on a fresh PC

When I clone this scaffold onto a new machine:

1. **Install Node** (for `npx`). Already needed for many other dev
   tools.
2. **Install Claude Code** (and/or Codex CLI). Already part of my
   stack.
3. From the project root, add the Playwright MCP server:
   ```powershell
   claude mcp add --scope project playwright -- npx -y "@playwright/mcp@latest" --headed
   ```
4. First time the MCP fires, let `npx` download the package and
   `playwright install chromium` install the browser. ~150 MB, one-time.
5. Boot the app:
   ```powershell
   .\launch_app.bat
   ```
6. Ask the agent to verify a known behavior. If a Chromium window pops
   up and the agent reports back, the loop is wired.

That's it. There's no per-project Playwright config, no test runner to
configure, no CI integration. The whole point is that the verification
loop is *zero-config infrastructure that lives outside the repo*.

---

## Troubleshooting

| Symptom                                             | Likely cause / fix                                                                                |
| --------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| Chromium window doesn't appear                      | MCP launched headless. Check `--headed` is in the `claude mcp add` command. `claude mcp list` to inspect. |
| `npx` hangs on first run                            | First-time package download. Wait once; cached afterward.                                          |
| Agent can't find a widget                           | Missing or unstable `key=`. Add one. Then rerun.                                                   |
| Streamlit tab shows blank in the browser            | Streamlit didn't finish reruning. Tell the agent to `wait_for` a known element before snapshotting.|
| Same test passes locally, fails in CI               | CI is headless, local is headed. Some Streamlit components render slightly differently. Use `wait_for_selector` aggressively. |
| Agent loops forever                                  | No action cap in the prompt. Always include "≤ N actions, then report."                           |
| Screenshot is huge in tokens                         | Vision tokens are expensive. Snapshot instead. Screenshot only on failure.                         |

---

## Why this lives in the scaffold (not in each project)

Browser testing is **infrastructure that should be the same across all
my projects**. Per-project variation = drift = "wait, how did I set this
up last time?" The scaffold's CLAUDE.md mirrors the testing rules into
every clone, and this doc is the single canonical explanation. When I
update the recipe, I update it here.

The corresponding rules in `docs/agents/CLAUDE.master.md` (the **End-to-end
UI testing** section) are the *enforcement* layer: they tell every AI
agent that drops into a downstream repo what to do and what not to do.
This doc is the *teaching* layer: it tells *me* why those rules exist.
