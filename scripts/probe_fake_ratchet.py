"""Ratchet on per-platform fake-probe tests: a process, port or listener probe is pinned on every platform.

A probe that reads processes, ports or listeners behaves differently on each
operating system (macOS framework Python changes its image path after launch,
a listener scan can be refused or partial, a listener may be bound to another
address). The gate runs on Windows only, so the macOS shape is found a night
later, on the Mac nightly. The rule (docs/probe-fake-ratchet.md): each probe
module the repo declares has a test that feeds it each supported platform's
probe output through a seam, with a fake, and a change to a probe module ships
with a change to such a test.

The rules:

  * `probe-fake-missing` -- a declared probe module has no test module that
    imports it and fakes one of the declared platforms. One hit per module and
    platform; existing gaps sit in the baseline and only shrink.
  * `probe-changed-no-fake` -- the branch's diff against the merge base with
    `base_ref` (committed, staged, unstaged and untracked) changes a probe
    module but no test module that imports it. A diff hit belongs to one change
    and is never baselined.
  * `bare-exempt` -- a `probe-fake-exempt` marker with no reason; it exempts
    nothing and is itself a hit.

A probe module carrying `probe-fake-exempt: <reason>` is exempt from
`probe-fake-missing`; the marker among the lines this diff adds to it exempts
the diff from `probe-changed-no-fake`. Every exemption is printed on every run.

Whenever the diff touches a probe module the run prints
`MAC_NIGHTLY: unverified (...)`: the fakes stand in for the Mac until the next
night, and the finish report names the Mac nightly as unverified, never passed.

Opt-in, per adopter, in its own `.fleet.toml`:

    [probe_fake_ratchet]
    enabled    = true
    baseline   = ".ratchets/probe-fake.json"   # default
    probes     = ["src/ports.py", "src/proc/*.py"]  # globs; required
    platforms  = ["win32", "darwin"]            # default; sys.platform values
    test_paths = ["tests/**"]                   # default
    base_ref   = "origin/main"                  # default

Exit codes: 0 pass or not enabled; 1 a hit beyond the baseline, a diff hit, or
the baseline is missing / a mode was refused; 2 bad configuration, baseline or
arguments; 3 NOT CONFIRMED (git cannot list the tree or find the merge base, a
file cannot be read or does not parse) -- the gate fails on it. Modes: `check`,
`--init`, `--update-baseline` (shrink only), `--update-baseline
--allow-additions` (shows in review).

Vendor-verbatim (manifest key `probe_fake_ratchet`), stdlib-only.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import importlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any


def _load_sibling(name: str) -> ModuleType:
    """A sibling module, loaded by path so `python -P` (scripts/ off `sys.path`) finds it."""
    try:
        if __package__:  # imported as a package member (the tests)
            return importlib.import_module(f"{__package__}.{name}")
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{name}.py"))
        assert spec is not None and spec.loader is not None
        module = sys.modules[spec.name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except (ImportError, OSError, SyntaxError):
        print(f"probe fake ratchet: NOT CONFIRMED: {name}.py is missing beside this script -- vendor both "
              "(manifest keys probe_fake_ratchet and ratchet_baseline)")
        sys.exit(3)


_baseline = _load_sibling("ratchet_baseline")  # where the baseline lives, and its move into .ratchets/

REPO_ROOT = Path(__file__).resolve().parent.parent

RULES: tuple[str, ...] = ("probe-fake-missing", "probe-changed-no-fake", "bare-exempt")
BASELINE_VERSION = 1
DEFAULT_BASELINE = ".ratchets/probe-fake.json"
DEFAULT_PLATFORMS: tuple[str, ...] = ("win32", "darwin")
DEFAULT_TEST_PATHS: tuple[str, ...] = ("tests/**",)
DEFAULT_BASE_REF = "origin/main"
LABEL = "probe fake ratchet"
EXIT_PASS, EXIT_RISE, EXIT_CONFIG, EXIT_NOT_CONFIRMED = 0, 1, 2, 3
GIT_TIMEOUT_S = 60
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# The words a test uses for each platform, in its names and string data, compared as whole
# identifier or word tokens so `window` or `machine` never count.
PLATFORM_WORDS: dict[str, frozenset[str]] = {
    "win32": frozenset({"win32", "win64", "win", "windows", "nt"}),
    "darwin": frozenset({"darwin", "macos", "mac", "osx"}),
    "linux": frozenset({"linux"}),
}

_CONFIG_KEYS = {"enabled", "baseline", "probes", "platforms", "test_paths", "base_ref"}
_EXEMPT = re.compile(r"probe-fake-exempt\b:?[ \t]*(.*)")
_TOKEN = re.compile(r"[A-Z]+(?![a-z])|[A-Za-z][a-z]*|\d+")
# Calls whose string arguments say why a test does not run, never what it fakes.
_SKIP_CALLS = frozenset({"skip", "skipif", "xfail", "importorskip"})


class ConfigError(RuntimeError):
    """The adopter's configuration or baseline is unusable (exit 2)."""


class NotConfirmed(RuntimeError):
    """git or a file could not be read, so the result is unknown (exit 3)."""


# ----------------------------------------------------------------- config

def _str_list(table: dict[str, Any], key: str, default: tuple[str, ...]) -> list[str]:
    value = table.get(key, list(default))
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ConfigError(f"[probe_fake_ratchet] {key} must be a list of non-empty strings")
    return list(value)


def parse_config(data: dict[str, Any]) -> dict[str, Any] | None:
    """`[probe_fake_ratchet]` -> its settings, or None when the stage is not enabled."""
    table = data.get("probe_fake_ratchet")
    if table is None:
        return None
    if not isinstance(table, dict):
        raise ConfigError("[probe_fake_ratchet] must be a table")
    unknown = sorted(set(table) - _CONFIG_KEYS)
    if unknown:
        raise ConfigError(f"[probe_fake_ratchet]: unknown key(s) {', '.join(unknown)}")
    enabled = table.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("[probe_fake_ratchet] enabled must be true or false")
    if not enabled:
        return None
    baseline = _baseline.configured(table.get("baseline", DEFAULT_BASELINE), DEFAULT_BASELINE)
    if not isinstance(baseline, str) or not baseline:
        raise ConfigError("[probe_fake_ratchet] baseline must be a non-empty string")
    probes = _str_list(table, "probes", ())
    if not probes:
        raise ConfigError("[probe_fake_ratchet] probes must name at least one probe module glob")
    platforms = _str_list(table, "platforms", DEFAULT_PLATFORMS)
    bad = sorted(set(platforms) - set(PLATFORM_WORDS))
    if bad:
        raise ConfigError(f"[probe_fake_ratchet] unknown platform(s) {', '.join(bad)} "
                          f"(known: {', '.join(PLATFORM_WORDS)})")
    base_ref = table.get("base_ref", DEFAULT_BASE_REF)
    if not isinstance(base_ref, str) or not base_ref:
        raise ConfigError("[probe_fake_ratchet] base_ref must be a non-empty string")
    return {"baseline": baseline, "probes": probes, "platforms": platforms,
            "test_paths": _str_list(table, "test_paths", DEFAULT_TEST_PATHS), "base_ref": base_ref}


def read_toml(root: Path) -> dict[str, Any]:
    path = root / ".fleet.toml"
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc


# -------------------------------------------------------------------- git

def _git(root: Path, args: list[str]) -> str:
    """git's stdout; NotConfirmed when git cannot run or fails, since nothing is known then."""
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")  # a read-only check must not leave index locks
    try:
        # git-wrapper-exempt: a stdlib script that ports to other repos; it sets GIT_OPTIONAL_LOCKS=0 itself
        proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", stdin=subprocess.DEVNULL, env=env, creationflags=_NO_WINDOW,
                              timeout=GIT_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise NotConfirmed(f"git {args[0]} could not run: {exc}") from exc
    if proc.returncode != 0:
        raise NotConfirmed(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.returncode}")
    return proc.stdout


def tree_files(root: Path) -> list[str]:
    """Tracked and untracked-but-not-ignored files, POSIX-relative."""
    out = _git(root, ["ls-files", "--cached", "--others", "--exclude-standard"])
    return sorted({line.strip() for line in out.splitlines() if line.strip()})


def changed_files(root: Path, base_ref: str) -> tuple[set[str], set[str], str]:
    """(files the branch changes against its merge base with `base_ref`, the untracked ones, that merge base)."""
    try:
        base = _git(root, ["merge-base", "HEAD", base_ref]).strip()
    except NotConfirmed as exc:
        raise NotConfirmed(f"no merge base with {base_ref} ({exc}); fetch it or set base_ref") from exc
    diffed = {p.strip() for p in _git(root, ["diff", "--name-only", "--no-renames", base]).splitlines() if p.strip()}
    untracked = {p.strip() for p in _git(root, ["ls-files", "--others", "--exclude-standard"]).splitlines()
                 if p.strip()}
    return diffed | untracked, untracked, base


def added_lines(root: Path, base: str, rel: str, untracked: bool) -> list[str]:
    """The lines the branch adds to `rel` against `base`: the whole file when git does not track it yet."""
    if untracked:
        return read_text(root, rel).splitlines()
    out = _git(root, ["diff", "--unified=0", "--no-color", base, "--", rel])
    return [line[1:] for line in out.splitlines() if line.startswith("+") and not line.startswith("+++")]


# ------------------------------------------------------------------ rules

def read_text(root: Path, rel: str) -> str:
    try:
        return (root / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise NotConfirmed(f"{rel}: cannot be read: {exc}") from exc


def _matches(rel: str, globs: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(rel, g) or PurePosixPath(rel).match(g) for g in globs)


def is_test_module(rel: str, test_paths: list[str]) -> bool:
    name = PurePosixPath(rel).name
    return (name.startswith("test_") or name.endswith("_test.py")) and name.endswith(".py") \
        and _matches(rel, test_paths)


def module_names(rel: str) -> set[str]:
    """Dotted names a test may import `rel` by: from the repo root, and from under a `src/` layout."""
    parts = list(PurePosixPath(rel).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    names = {".".join(parts)} if parts else set()
    if len(parts) > 1 and parts[0] == "src":
        names.add(".".join(parts[1:]))
    return names


def _imported(tree: ast.Module) -> set[str]:
    """Every dotted name the module imports, with `from a import b` also giving `a.b`."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _docstrings(tree: ast.Module) -> set[int]:
    ids: set[int] = set()
    for node in [tree, *ast.walk(tree)]:
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


def _skip_strings(tree: ast.Module) -> set[int]:
    """String constants inside a skip, skipif, xfail or importorskip call."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
            if name in _SKIP_CALLS:
                ids.update(id(n) for n in ast.walk(node) if isinstance(n, ast.Constant))
    return ids


def _words(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN.findall(text)}


def module_facts(text: str, rel: str) -> tuple[set[str], set[str], set[str]]:
    """(imported dotted names, string constants, words in identifiers and string data) of one test module."""
    try:
        tree = ast.parse(text, filename=rel)
    except SyntaxError as exc:
        raise NotConfirmed(f"{rel}: does not parse: {exc.msg} (line {exc.lineno})") from exc
    skipped = _docstrings(tree) | _skip_strings(tree)
    strings: set[str] = set()
    words: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skipped:
            strings.add(node.value)
            words |= _words(node.value)
        elif isinstance(node, ast.Name):
            words |= _words(node.id)
        elif isinstance(node, ast.Attribute):
            words |= _words(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            words |= _words(node.name)
        elif isinstance(node, ast.arg):
            words |= _words(node.arg)
    return _imported(tree), strings, words


def fakes(facts: tuple[set[str], set[str], set[str]], probe: str) -> bool:
    """True when the test module reaches `probe`: imports it, or names its path or dotted name in a string."""
    imported, strings, _ = facts
    names = module_names(probe)
    if imported & names:
        return True
    return any(probe in s or any(n == s or s.startswith(n + ".") for n in names) for s in strings)


def exemption(text: str) -> str | None:
    """The reason of the probe module's `probe-fake-exempt` marker: '' for a bare marker, None for none."""
    for line in text.splitlines():
        match = _EXEMPT.search(line)
        if match:
            return match.group(1).strip()
    return None


def scan(root: Path, cfg: dict[str, Any], *, with_diff: bool = True) -> tuple[dict[str, str], list[str], list[str]]:
    """(hits {key: why}, exemption notes, probe modules the diff touches)."""
    files = tree_files(root)
    probes = [f for f in files if f.endswith(".py") and _matches(f, cfg["probes"])]
    tests = {f: module_facts(read_text(root, f), f) for f in files if is_test_module(f, cfg["test_paths"])}
    hits: dict[str, str] = {}
    notes: list[str] = []
    for probe in probes:
        reason = exemption(read_text(root, probe))
        if reason == "":
            hits[f"bare-exempt: {probe}"] = "`probe-fake-exempt` needs a reason after the colon"
        elif reason is not None:
            notes.append(f"{probe}: probe-fake-missing exempt: {reason}")
            continue
        reaching = [facts for facts in tests.values() if fakes(facts, probe)]
        for platform in cfg["platforms"]:
            if not any(facts[2] & PLATFORM_WORDS[platform] for facts in reaching):
                why = ("no test module imports it" if not reaching
                       else f"no test that imports it fakes {platform} (names or data never say "
                            f"{'/'.join(sorted(PLATFORM_WORDS[platform]))})")
                hits[f"probe-fake-missing: {probe} [{platform}]"] = why
    touched: list[str] = []
    if with_diff:
        changed, untracked, base = changed_files(root, cfg["base_ref"])
        changed_tests = [tests[f] for f in changed if f in tests]
        for probe in (p for p in probes if p in changed):
            touched.append(probe)
            added = added_lines(root, base, probe, probe in untracked)
            reason = next((m.group(1).strip() for m in map(_EXEMPT.search, added) if m), None)
            if reason:
                notes.append(f"{probe}: probe-changed-no-fake exempt in this diff: {reason}")
            elif not any(fakes(facts, probe) for facts in changed_tests):
                hits[f"probe-changed-no-fake: {probe}"] = (
                    f"changed against {cfg['base_ref']} with no change to a test module that imports it")
    return hits, notes, touched


# ---------------------------------------------------------------- baseline

def read_baseline(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read baseline {path}: {exc}") from exc
    except ValueError as exc:
        raise ConfigError(f"baseline {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != BASELINE_VERSION:
        raise ConfigError(f"baseline {path}: expected an object with version {BASELINE_VERSION}")
    hits = data.get("hits")
    if not isinstance(hits, list) or not all(isinstance(h, str) and h for h in hits):
        raise ConfigError(f"baseline {path}: `hits` must be a list of non-empty strings")
    return sorted(set(hits))


def render_baseline(hits: list[str]) -> str:
    """Sorted, one hit per line, so a baseline diff reviews cleanly."""
    lines = ",\n".join("    " + json.dumps(h, ensure_ascii=False) for h in sorted(set(hits)))
    body = "[\n" + lines + "\n  ]" if lines else "[]"
    return f'{{\n  "version": {BASELINE_VERSION},\n  "hits": {body}\n}}\n'


def _baselinable(hits: dict[str, str]) -> list[str]:
    """A diff hit belongs to one change, so it never enters the baseline."""
    return sorted(h for h in hits if not h.startswith("probe-changed-no-fake:"))


# -------------------------------------------------------------------- CLI

def _report(added: list[str], hits: dict[str, str]) -> None:
    print(f"{LABEL}: {len(added)} hit(s) beyond the baseline:")
    for hit in added:
        print(f"  [{hit.split(':')[0]}] {hit.partition(': ')[2] or hit}: {hits[hit]}")


def _load_baseline(path: Path, name: str, init: bool) -> list[str] | int:
    if init:
        if path.exists():
            print(f"{LABEL}: {name} already exists; --init never overwrites it")
            return EXIT_CONFIG
        return []
    if not path.is_file():
        print(f"{LABEL}: FAIL: baseline {name} is missing -- run `python scripts/probe_fake_ratchet.py --init`")
        return EXIT_RISE
    try:
        return read_baseline(path)
    except ConfigError as exc:
        print(f"{LABEL}: BASELINE ERROR: {exc}")
        return EXIT_CONFIG


def _write(path: Path, hits: list[str]) -> None:
    # runtime-write-exempt: the repo's own baseline file, named by the config, never a code folder
    path.write_text(render_baseline(hits), encoding="utf-8", newline="\n")


def run(root: Path, *, init: bool, update: bool, allow_additions: bool) -> int:
    try:
        cfg = parse_config(read_toml(root))
    except ConfigError as exc:
        print(f"{LABEL}: CONFIG ERROR: {exc}")
        return EXIT_CONFIG
    if cfg is None:
        if init or update:
            print(f"{LABEL}: not enabled -- set [probe_fake_ratchet] enabled = true in .fleet.toml first")
            return EXIT_CONFIG
        print(f"{LABEL}: not enabled (no [probe_fake_ratchet] enabled = true in .fleet.toml)")
        return EXIT_PASS
    baseline_path = _baseline.resolve(root, cfg["baseline"], DEFAULT_BASELINE, LABEL)
    if baseline_path is None:
        return EXIT_CONFIG
    baseline = _load_baseline(baseline_path, cfg["baseline"], init)
    if isinstance(baseline, int):
        return baseline
    try:
        hits, notes, touched = scan(root, cfg, with_diff=not (init or update))
    except NotConfirmed as exc:
        print(f"{LABEL}: NOT CONFIRMED: {exc}")
        print("  (the tree or the diff could not be read, so this is neither a pass nor a hit)")
        return EXIT_NOT_CONFIRMED
    for note in notes:
        print(f"  exempt: {note}")
    stored = _baselinable(hits)

    if init:
        _write(baseline_path, stored)
        print(f"{LABEL}: wrote {cfg['baseline']} with {len(stored)} hit(s)")
        return EXIT_PASS
    if update:
        added = [h for h in stored if h not in baseline]
        if added and not allow_additions:
            _report(added, hits)
            print(f"{LABEL}: nothing written -- add the fake test, or raise the baseline with --allow-additions")
            return EXIT_RISE
        _write(baseline_path, stored)
        gone = [h for h in baseline if h not in stored]
        print(f"{LABEL}: {cfg['baseline']} now has {len(stored)} hit(s) ({len(gone)} dropped, {len(added)} added)")
        return EXIT_PASS
    if touched:
        print(f"MAC_NIGHTLY: unverified (this diff changes probe module(s) {', '.join(touched)}; "
              "per-platform fakes stand in until the next Mac nightly is green)")
    added = sorted(h for h in hits if h not in baseline)
    gone = sorted(h for h in baseline if h not in hits)
    if added:
        _report(added, hits)
        print("  A probe change ships a test that pins each platform's probe output with a fake "
              "(docs/probe-fake-ratchet.md). Or (reviewed) raise the baseline with "
              "`--update-baseline --allow-additions`; a diff hit is never baselined.")
        return EXIT_RISE
    print(f"{LABEL}: PASS ({len(hits)} baselined hit(s), none new)")
    if gone:
        print(f"  {len(gone)} baselined hit(s) are fixed -- run "
              "`python scripts/probe_fake_ratchet.py --update-baseline` to lower it")
    return EXIT_PASS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repo root (default: this repo)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--init", action="store_true", help="write the first baseline; never overwrites")
    mode.add_argument("--update-baseline", action="store_true", help="drop fixed hits (shrink only)")
    parser.add_argument("--allow-additions", action="store_true",
                        help="with --update-baseline: also add hits (shows in review)")
    args = parser.parse_args(argv)
    if args.allow_additions and not args.update_baseline:
        parser.error("--allow-additions only applies with --update-baseline")
    return run(args.root.resolve(), init=args.init, update=args.update_baseline,
               allow_additions=args.allow_additions)


if __name__ == "__main__":
    # Reports contain non-ASCII; a cp1252 console would otherwise crash after the verdict.
    for _stream in (sys.stdout, sys.stderr):
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # real console streams are TextIOWrapper
    sys.exit(main())
