#!/usr/bin/env python3
"""
profile_freshness.py — skip analyze_competitors.py when nothing changed.

`scripts/analyze_competitors.py` is ~3 min of GPU work to rebuild the 4 profile
JSONs from data/competitors/**. It only needs to re-run when the competitor
corpus or the analyzer itself changes. This helper hashes both, persists the
hash in autoresearch-state.json, and no-ops when nothing is new.

Usage:
    python scripts/profile_freshness.py            # check + rebuild if stale
    python scripts/profile_freshness.py --check    # print status, exit 0 if fresh, 1 if stale
    python scripts/profile_freshness.py --force    # always rebuild and update hash

The autoresearch loop's driver calls ensure_profiles_fresh() before every
iteration; it is cheap when nothing has changed (a directory walk + a few
sha256 digests) and saves ~3 min when it skips a rebuild.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PROJECT_ROOT = _HERE.parent
STATE_PATH = PROJECT_ROOT / "autoresearch-state.json"
COMPETITORS_DIR = PROJECT_ROOT / "data" / "competitors"
PROFILES_DIR = PROJECT_ROOT / "data" / "profiles"
ANALYZER = _HERE / "analyze_competitors.py"
KEYWORDS_CFG = PROJECT_ROOT / "data" / "keywords.json"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_competitor_hash() -> str:
    """Deterministic hash of every competitor .md + the analyzer script.

    Sorts paths to make hash stable across filesystems. Reads file contents
    (not mtimes) so touch(1) doesn't spuriously invalidate the cache.
    """
    h = hashlib.sha256()

    if ANALYZER.exists():
        h.update(b"analyzer:")
        h.update(_sha256_file(ANALYZER).encode())
        h.update(b"\n")

    if COMPETITORS_DIR.exists():
        for p in sorted(COMPETITORS_DIR.rglob("*.md")):
            rel = p.relative_to(COMPETITORS_DIR).as_posix()
            h.update(f"{rel}:".encode())
            h.update(_sha256_file(p).encode())
            h.update(b"\n")

    return h.hexdigest()


def expected_profile_paths() -> list[Path]:
    """Read keywords.json to derive the expected profile file list."""
    if not KEYWORDS_CFG.exists():
        return []
    with open(KEYWORDS_CFG, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return [PROFILES_DIR / f"{kw['slug']}-profile.json" for kw in cfg.get("keywords", [])]


def all_profiles_exist() -> bool:
    paths = expected_profile_paths()
    return bool(paths) and all(p.exists() for p in paths)


def _sync_state_path() -> None:
    """Tests monkeypatch this module's STATE_PATH; mirror to score_cache so
    both modules agree on the file they read/write."""
    import score_cache
    score_cache.STATE_PATH = STATE_PATH


def read_stored_hash() -> str | None:
    _sync_state_path()
    import score_cache
    return score_cache.read_state().get("competitor_hash")


def write_stored_hash(digest: str) -> None:
    _sync_state_path()
    import score_cache
    with score_cache.mutate_state() as state:
        state["competitor_hash"] = digest


def is_fresh() -> tuple[bool, str, str | None]:
    """Returns (fresh, current_hash, stored_hash)."""
    stored = read_stored_hash()
    current = compute_competitor_hash()
    fresh = (stored == current) and all_profiles_exist()
    return fresh, current, stored


def run_analyzer() -> int:
    """Invoke scripts/analyze_competitors.py; return its exit code."""
    env = os.environ.copy()
    cmd = [sys.executable, str(ANALYZER)]
    print(f"profile_freshness: running {' '.join(cmd)}", file=sys.stderr, flush=True)
    proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), env=env)
    return proc.returncode


def ensure_profiles_fresh(force: bool = False) -> dict:
    """Top-level entry point for the loop driver.

    Returns a small status dict:
        {"action": "skipped"|"rebuilt"|"failed", "hash": "<sha>"}

    Rebuilds profiles when:
      - force=True
      - any expected profile file is missing
      - the stored hash doesn't match the current corpus + analyzer hash
    """
    fresh, current, stored = is_fresh()
    if fresh and not force:
        print(
            f"profile_freshness: up to date (hash={current[:12]}...); skipping analyze_competitors",
            file=sys.stderr,
            flush=True,
        )
        return {"action": "skipped", "hash": current}

    why = "forced" if force else ("no stored hash" if stored is None else
          "hash mismatch" if stored != current else "missing profile file")
    print(
        f"profile_freshness: rebuild required ({why}); stored={stored[:12] if stored else None}..., current={current[:12]}...",
        file=sys.stderr,
        flush=True,
    )
    rc = run_analyzer()
    if rc != 0:
        return {"action": "failed", "hash": current, "returncode": rc}

    # Re-hash after analyze_competitors; the analyzer itself should not have
    # changed mid-run, so current should still match — but rehash to be safe.
    new_current = compute_competitor_hash()
    write_stored_hash(new_current)
    return {"action": "rebuilt", "hash": new_current}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="Print status; exit 0 if fresh, 1 if stale. Do not rebuild.")
    parser.add_argument("--force", action="store_true",
                        help="Always rebuild profiles and refresh the stored hash.")
    args = parser.parse_args()

    if args.check:
        fresh, current, stored = is_fresh()
        print(f"current_hash: {current}")
        print(f"stored_hash:  {stored}")
        print(f"profiles_present: {all_profiles_exist()}")
        print(f"fresh: {fresh}")
        return 0 if fresh else 1

    result = ensure_profiles_fresh(force=args.force)
    print(json.dumps(result))
    return 0 if result["action"] != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
