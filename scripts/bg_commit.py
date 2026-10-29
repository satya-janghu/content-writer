"""
bg_commit.py — detached git commit + TSV logging for the autoresearch loop.

Spawns a child shell with start_new_session=True so the driver doesn't block
on git hooks, push, or slow filesystem syncs. Logs go to a per-PID file under
/tmp so they can be inspected post-hoc without polluting the parent console.

Usage from the loop driver:
    from bg_commit import commit_in_background, append_tsv_in_background

    commit_in_background(
        message="iter 37 keep — seo-for-saas 0.872 -> 0.879",
        paths=["rules/content-generation/SKILL.md",
               "content/seo-for-saas.md"],
    )
    append_tsv_in_background(
        "autoresearch-results.tsv",
        row=[iter_no, slug, prev_score, new_score, decision, dim_changed],
    )
"""
from __future__ import annotations

import csv
import io
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _log_path() -> str:
    return f"/tmp/bg_commit-{os.getpid()}-{int(time.time())}.log"


def commit_in_background(message: str, paths: list[str],
                         cwd: str | None = None) -> subprocess.Popen:
    """Run git add + git commit asynchronously. Returns the Popen handle.

    The driver must NOT wait on this Popen unless the next step literally
    depends on the commit being visible (it almost never does — the next
    iteration reads from the working tree and state.json, not from git).

    On the off chance the driver needs to wait: `proc.wait(timeout=60)`.
    """
    if not paths:
        raise ValueError("commit_in_background requires at least one path")

    workdir = cwd or str(PROJECT_ROOT)
    log = _log_path()
    # Chain git add + git commit through one shell invocation so both
    # commands see the same staged state and the commit fires only if
    # add succeeded.
    add_args = " ".join(f"'{p}'" for p in paths)
    msg_escaped = message.replace("'", "'\\''")
    cmd = f"git add -- {add_args} && git commit -m '{msg_escaped}'"

    return subprocess.Popen(
        ["bash", "-lc", cmd],
        cwd=workdir,
        stdout=open(log, "a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,  # survive parent exit
    )


def append_tsv(tsv_path: str, row: list) -> None:
    """Append one TSV row with an advisory lock, synchronously.

    TSV appends are microseconds; doing them in-process with fcntl.flock is
    simpler than a detached subprocess and avoids shell-quoting newlines.
    Safe across parallel worker processes on the same host.
    """
    import fcntl
    target = tsv_path if os.path.isabs(tsv_path) else str(PROJECT_ROOT / tsv_path)
    buf = io.StringIO()
    csv.writer(buf, delimiter="\t", lineterminator="\n").writerow(row)
    line = buf.getvalue()
    with open(target, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


# Alias kept for existing callers.
append_tsv_in_background = append_tsv


def main() -> int:
    """Smoke test — called directly to prove the module works."""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--tsv-smoke", action="store_true",
                        help="Append a dummy row to /tmp/bg_commit_smoke.tsv")
    args = parser.parse_args()

    if args.tsv_smoke:
        path = "/tmp/bg_commit_smoke.tsv"
        append_tsv(path, ["ts", str(int(time.time())), "smoke", "ok"])
        print(f"appended to {path}")
        with open(path, "r", encoding="utf-8") as f:
            print(f.read())
        return 0

    print("nothing to do (try --tsv-smoke)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
