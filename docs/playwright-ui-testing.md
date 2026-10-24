# End-to-end UI testing with Playwright

The fleet-wide reasoning, setup, and bootstrap recipe for browser testing —
the two-loop split (headed agent verification vs. headless pytest-playwright
regression), MCP setup per agent, default-timeout conventions, the bounded
WebKit driver teardown, freeing a pinned directory, and the rendered-geometry
design-conformance helper — lives in one canonical place:
[`project-scaffolding`'s `docs/playwright-ui-testing.md`](https://github.com/ferraroroberto/project-scaffolding/blob/main/docs/playwright-ui-testing.md).
Read that doc first; this file only adds what's specific to this repo.

## What's specific to home-automation

- The two loops, the verification-suite rules, the `E2E_LIVE` live-tray-adopt
  guard, and the current runtime contract (test count, wall-clock budget) are
  documented in this repo's own `CLAUDE.md` under "End-to-end UI testing" and
  "Verification" — that's the up-to-date source for this repo's numbers, not
  this file.
- `scripts/unpin_directory.py` and `tests/e2e/conftest.py`'s bounded teardown
  (`E2E_TEARDOWN_TIMEOUT_S`) are this repo's concrete implementations of the
  scaffold doc's "bounded WebKit driver teardown" and "freeing a pinned
  directory" sections — read those sections there for the *why*, these files
  here for the *how it's wired in this repo*.
- Wedged WebKit helpers: on Playwright 1.60 / webkit-2287 a
  per-context `WebKitNetworkProcess` occasionally wedged inside termination
  at an ordinary per-test `context.close()`. That is a WebKit-side wedge the
  repo can't prevent or reap, and the next `browser.close()` then hung until
  the teardown watchdog fired. 1.61 / webkit-2311 measured clean (0 in 5 full
  runs vs. 2 in 4), so `requirements-dev.txt` floors `playwright>=1.61`. If
  wedges return, the gate's `browser helpers newly wedged during this run`
  line and the `teardown watchdog fired` breadcrumb in
  `webapp/verify-progress.log` say so per run.
- Mobile projection: the regression suite runs `test_design_matrix.py`'s
  geometry matrix (4 viewports × 2 themes) across Chromium + WebKit — see
  `tests/e2e/_geometry.py`.
