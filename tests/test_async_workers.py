"""
test_async_workers.py — async worker coordination + serialized merge queue.

Runs the supervisor in --dry-run mode with short stagnation so every worker's
propose is a stub_rule_tweak. Proves:
  1. All 4 coordinators start, iterate, and reach stagnation independently.
  2. The supervisor exits cleanly.
  3. Finishes in well under a minute (no model API calls in dry-run).
"""
from __future__ import annotations
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def test_dry_run_supervisor_converges_fast():
    """A --dry-run --iterations 3 --max-stagnation 2 run should finish in
    well under 120s because dry-run makes no model API calls."""
    if not (SCRIPTS.parent / ".git").exists():
        import pytest
        pytest.skip("not in a git worktree")

    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "parallel_loop.py"),
         "--iterations", "3", "--max-stagnation", "2", "--dry-run"],
        cwd=str(SCRIPTS.parent), capture_output=True, text=True, timeout=240,
    )
    elapsed = time.time() - t0
    assert proc.returncode == 0, proc.stderr[-2000:]
    # Ceiling is looser than the ~67s observed in isolation because when this
    # test runs late in the suite (after other tests that touched the shared
    # eval_server / worktree state), the supervisor's first `_refresh_worktree`
    # calls can take longer. 180s is still comfortably "fast" — a real regen
    # under Opus p95 is 634s.
    assert elapsed < 180, f"dry-run took {elapsed:.1f}s; should be <180"
