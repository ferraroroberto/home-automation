"""Ratchet on decision-history narration inside product-code comments/docstrings.

Audit #1044 (two consecutive `claude-md-drift` findings): a module header or
inline comment that narrates how the code got here -- "used to be duplicated",
"the copies had drifted", "Extracted verbatim from X", "This was a pure code
move" -- teaches the next refactor to add another paragraph of the same shape.
That story belongs in the issue, the PR and `git log`; a comment states why the
code is the way it is *now* (global `~/.claude/CLAUDE.md`, "Comments state the
present-tense why"). `comment_ratchet.py` (vendored) only counts `#NNN`
references and skips docstrings entirely, so this narration pattern passes it
clean -- this stage is the structural check that closes that gap.

This stage counts, per product file, how many of a fixed set of history-marker
phrases appear in its text, compares the count with a committed baseline, and
fails only when a file's count *rises*. Existing debt never blocks work, but it
cannot grow.

Markers (case-insensitive, word-bounded): "used to be", "extracted verbatim",
"pure code move", "previously recorded", "had drifted". Chosen from the exact
phrasing the audit quoted -- high-precision, narrow on purpose, so the stage
never fires on a legitimate present-tense comment.

Unlike `comment_ratchet.py`, this stage does not parse out comments/docstrings
with a real tokenizer -- it scans each in-scope file's whole text. The chosen
markers are narration phrases that in practice occur only in prose (a
docstring or a comment), never in executable code or a short machine-facing
string, so the simplification does not change what gets caught; it does mean
a marker phrase inside a long user-facing string literal would also count,
which has not happened here.

Opt-in via `.fleet.toml`:

    [history_marker_ratchet]
    enabled  = true
    baseline = ".history-marker-ratchet-baseline.json"   # default
    include  = ["app/", "src/"]                           # path prefixes; default: whole tree
    exclude  = ["**/generated/**"]                         # globs, added to the built-in set

Product code only: tracked/untracked-but-not-ignored files (`git ls-files
--cached --others --exclude-standard`) with a `.py`/`.js`/`.mjs`/`.cjs`/`.css`
suffix, minus tests (`tests/`, `test_*.py`, `*_test.py`, `conftest.py`,
`*.test.js`), `*.min.*`, `node_modules`, venvs, files whose first lines say
`DO NOT EDIT` or `@generated`, and every `dest` in the adopter's own
`[vendored]` table (a vendored file's prose is project-scaffolding's call, not
this repo's).

Exit codes (a state is never folded into a pass):

  0  pass, or the stage is not enabled (declared, not inferred)
  1  a file's count rose, a new file carries markers, or the baseline is
     missing / a mode was refused
  2  bad configuration, baseline or arguments -- fail loud
  3  NOT CONFIRMED: `git` unusable or a file could not be read/decoded. The
     gate fails on it.

Modes: `check` (default; what the gate runs), `--init` (write the first
baseline, refuses to overwrite), `--update-baseline` (lower or drop entries;
exits 1 and writes nothing while any count rose), `--update-baseline
--allow-additions` (the explicit flag that raises or adds -- shows in review).

Repo-specific (not vendored): this repo's `src/` and `app/webapp/static/`
carry an unusually dense per-module "why" docstring/header convention (see
`docs/layout.md`), which is where the audit's narration debt accumulated;
other fleet repos have not shown the same pattern. A fleet-wide version of
this rule is a `project-scaffolding` decision, not this stage's.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent

BASELINE_VERSION = 1
DEFAULT_BASELINE = ".history-marker-ratchet-baseline.json"
BUILTIN_EXCLUDES = (
    "**/tests/**",
    "**/test_*.py",
    "**/*_test.py",
    "**/conftest.py",
    "**/*.test.js",
    "**/*.min.*",
    "**/node_modules/**",
    "**/.venv/**",
    "**/venv/**",
    "**/__pycache__/**",
)
GENERATED_MARKERS = ("DO NOT EDIT", "@generated")
GENERATED_HEAD_LINES = 5
GIT_TIMEOUT_S = 120
SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".css"}

EXIT_PASS, EXIT_RISE, EXIT_CONFIG, EXIT_NOT_CONFIRMED = 0, 1, 2, 3

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_CONFIG_KEYS = {"enabled", "baseline", "include", "exclude"}

# Exact phrasing a claude-md-drift audit quoted -- high-precision on purpose.
_MARKERS = (
    re.compile(r"\bused to be\b", re.IGNORECASE),
    re.compile(r"\bextracted verbatim\b", re.IGNORECASE),
    re.compile(r"\bpure code move\b", re.IGNORECASE),
    re.compile(r"\bpreviously recorded\b", re.IGNORECASE),
    re.compile(r"\bhad drifted\b", re.IGNORECASE),
)


class ConfigError(RuntimeError):
    """The `[history_marker_ratchet]` table or the baseline file is unusable."""


class NotConfirmed(RuntimeError):
    """A file could not be read/decoded, so its count is unknown."""


@dataclass(frozen=True)
class Config:
    baseline: str
    include: tuple[str, ...]
    exclude: tuple[str, ...]


@dataclass(frozen=True)
class Hit:
    line: int
    text: str


# ----------------------------------------------------------------- config

def _str_list(table: dict[str, object], name: str) -> tuple[str, ...]:
    value = table.get(name, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ConfigError(f"[history_marker_ratchet] {name} must be a list of non-empty strings")
    return tuple(value)


def parse_config(toml_text: str) -> Config | None:
    table = tomllib.loads(toml_text).get("history_marker_ratchet")
    if table is None:
        return None
    if not isinstance(table, dict):
        raise ConfigError("[history_marker_ratchet] must be a table")
    unknown = sorted(set(table) - _CONFIG_KEYS)
    if unknown:
        raise ConfigError(f"[history_marker_ratchet]: unknown key(s) {', '.join(unknown)}")
    enabled = table.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("[history_marker_ratchet] enabled must be true or false")
    if not enabled:
        return None
    baseline = table.get("baseline", DEFAULT_BASELINE)
    if not isinstance(baseline, str) or not baseline:
        raise ConfigError("[history_marker_ratchet] baseline must be a non-empty string")
    return Config(baseline, _str_list(table, "include"), _str_list(table, "exclude"))


def vendored_dests(toml_text: str) -> tuple[str, ...]:
    table = tomllib.loads(toml_text).get("vendored")
    if not isinstance(table, dict):
        return ()
    dests = []
    for entry in table.values():
        dest = entry.get("dest") if isinstance(entry, dict) else None
        if isinstance(dest, str) and dest.strip("/"):
            dests.append(dest.replace("\\", "/").strip("/"))
    return tuple(sorted(dests))


def load_config(root: Path) -> tuple[Config | None, tuple[str, ...]]:
    path = root / ".fleet.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        return parse_config(text), vendored_dests(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc


# ------------------------------------------------------------------- globs

def _glob_regex(glob: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def is_excluded(path: str, globs: tuple[str, ...]) -> bool:
    return any(_glob_regex(g).match(path) for g in globs)


def _under(path: str, prefix: str) -> bool:
    prefix = prefix.replace("\\", "/").strip("/")
    return path == prefix or path.startswith(prefix + "/")


def in_scope(path: str, cfg: Config, vendored: tuple[str, ...]) -> bool:
    if PurePosixPath(path).suffix.lower() not in SUFFIXES:
        return False
    if cfg.include and not any(_under(path, p) for p in cfg.include):
        return False
    if any(_under(path, d) for d in vendored):
        return False
    return not is_excluded(path, BUILTIN_EXCLUDES + cfg.exclude)


def is_generated(text: str) -> bool:
    head = "\n".join(text.split("\n", GENERATED_HEAD_LINES)[:GENERATED_HEAD_LINES])
    return any(marker in head for marker in GENERATED_MARKERS)


# ---------------------------------------------------------------- counting

def find_hits(text: str) -> list[Hit]:
    """Every history-marker phrase in `text`, with its 1-based line and the line's text."""
    lines = text.split("\n")
    hits: list[Hit] = []
    for marker in _MARKERS:
        for match in marker.finditer(text):
            line_no = text.count("\n", 0, match.start())
            hits.append(Hit(line_no + 1, lines[line_no].strip()))
    return hits


# ------------------------------------------------------------------- files

def tracked_files(root: Path) -> list[str]:
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=root, capture_output=True, env=env,
            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW, timeout=GIT_TIMEOUT_S, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise NotConfirmed(f"git ls-files timed out after {GIT_TIMEOUT_S}s") from exc
    except OSError as exc:
        raise NotConfirmed(f"git could not start: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise NotConfirmed(f"git ls-files exited {proc.returncode}: {detail[-1] if detail else '(no output)'}")
    return sorted(p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p)


def count_tree(root: Path, cfg: Config, vendored: tuple[str, ...]) -> dict[str, list[Hit]]:
    found: dict[str, list[Hit]] = {}
    for path in tracked_files(root):
        if not in_scope(path, cfg, vendored):
            continue
        try:
            raw = (root / path).read_bytes()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise NotConfirmed(f"{path}: cannot be read: {exc}") from exc
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise NotConfirmed(f"{path}: is not valid UTF-8: {exc}") from exc
        if is_generated(text):
            continue
        hits = find_hits(text.replace("\r\n", "\n"))
        if hits:
            found[path] = hits
    return found


# ---------------------------------------------------------------- baseline

def read_baseline(path: Path) -> dict[str, int]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read baseline {path}: {exc}") from exc
    except ValueError as exc:
        raise ConfigError(f"baseline {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != BASELINE_VERSION:
        raise ConfigError(f"baseline {path}: expected an object with version {BASELINE_VERSION}")
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise ConfigError(f"baseline {path}: `entries` must be a list")
    out: dict[str, int] = {}
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("key"), str)
            or isinstance(entry.get("count"), bool)
            or not isinstance(entry.get("count"), int)
            or entry["count"] < 1
        ):
            raise ConfigError(f"baseline {path}: malformed entry {entry!r}")
        out[entry["key"]] = entry["count"]
    return out


def render_baseline(counts: dict[str, int]) -> str:
    entries = [
        "    " + json.dumps({"key": key, "count": counts[key]}, ensure_ascii=False)
        for key in sorted(counts)
    ]
    body = "[\n" + ",\n".join(entries) + "\n  ]" if entries else "[]"
    return f'{{\n  "version": {BASELINE_VERSION},\n  "entries": {body}\n}}\n'


def diff(current: dict[str, int], baseline: dict[str, int]) -> tuple[list[str], list[str]]:
    rose = sorted(p for p, c in current.items() if c > baseline.get(p, 0))
    shrank = sorted(p for p, b in baseline.items() if current.get(p, 0) < b)
    return rose, shrank


# -------------------------------------------------------------------- CLI

def _report_rose(rose: list[str], current: dict[str, list[Hit]], baseline: dict[str, int]) -> None:
    print(f"history marker ratchet: {len(rose)} file(s) with MORE history-narration markers than the baseline:")
    for path in rose:
        was = baseline.get(path)
        print(f"  {path}: {len(current[path])} (baseline {was if was is not None else 'none -- not baselined'})")
        for hit in current[path]:
            print(f"      line {hit.line}: {hit.text}")


def run(root: Path, *, init: bool, update: bool, allow_additions: bool) -> int:
    try:
        cfg, vendored = load_config(root)
    except ConfigError as exc:
        print(f"history marker ratchet: CONFIG ERROR: {exc}")
        return EXIT_CONFIG
    if cfg is None:
        if init or update:
            print("history marker ratchet: not enabled -- set [history_marker_ratchet] enabled = true in .fleet.toml first")
            return EXIT_CONFIG
        print("history marker ratchet: not enabled (no [history_marker_ratchet] enabled = true in .fleet.toml)")
        return EXIT_PASS
    baseline_path = root / cfg.baseline

    baseline: dict[str, int] = {}
    if init:
        if baseline_path.exists():
            print(f"history marker ratchet: {cfg.baseline} already exists; --init never overwrites it")
            return EXIT_CONFIG
    elif baseline_path.is_file():
        try:
            baseline = read_baseline(baseline_path)
        except ConfigError as exc:
            print(f"history marker ratchet: BASELINE ERROR: {exc}")
            return EXIT_CONFIG
    else:
        print(f"history marker ratchet: FAIL: baseline {cfg.baseline} is missing -- run "
              "`python scripts/history_marker_ratchet.py --init`")
        return EXIT_RISE

    try:
        hits = count_tree(root, cfg, vendored)
    except NotConfirmed as exc:
        print(f"history marker ratchet: NOT CONFIRMED: {exc}")
        print("  (the count could not be established, so it is neither a pass nor a clean tree)")
        return EXIT_NOT_CONFIRMED
    current = {path: len(found) for path, found in hits.items()}

    if init:
        baseline_path.write_text(render_baseline(current), encoding="utf-8", newline="\n")
        print(f"history marker ratchet: wrote {cfg.baseline} with {len(current)} file(s), {sum(current.values())} marker(s)")
        return EXIT_PASS

    rose, shrank = diff(current, baseline)
    if update:
        if rose and not allow_additions:
            _report_rose(rose, hits, baseline)
            print("history marker ratchet: nothing written -- reword the markers, or raise the baseline with --allow-additions")
            return EXIT_RISE
        baseline_path.write_text(render_baseline(current), encoding="utf-8", newline="\n")
        print(f"history marker ratchet: {cfg.baseline} now has {len(current)} file(s), {sum(current.values())} marker(s) "
              f"({len(shrank)} lowered or dropped, {len(rose)} raised or added)")
        return EXIT_PASS

    if rose:
        _report_rose(rose, hits, baseline)
        print("  A comment/docstring states why the code is the way it is now; how it got here belongs in the "
              "issue, PR and git log. Reword it, or (reviewed) raise the baseline with "
              "`--update-baseline --allow-additions`.")
        return EXIT_RISE
    print(f"history marker ratchet: PASS ({sum(current.values())} marker(s) in {len(current)} file(s), none above the baseline)")
    if shrank:
        print(f"  {len(shrank)} file(s) are below the baseline -- run "
              f"`python scripts/history_marker_ratchet.py --update-baseline` to lower it")
    return EXIT_PASS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repo root (default: this repo)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--init", action="store_true", help="write the first baseline; never overwrites")
    mode.add_argument("--update-baseline", action="store_true", help="lower or drop entries (shrink only)")
    parser.add_argument("--allow-additions", action="store_true",
                        help="with --update-baseline: also raise or add entries (shows in review)")
    args = parser.parse_args(argv)
    if args.allow_additions and not args.update_baseline:
        parser.error("--allow-additions only applies with --update-baseline")
    return run(args.root.resolve(), init=args.init, update=args.update_baseline,
               allow_additions=args.allow_additions)


if __name__ == "__main__":
    sys.exit(main())
