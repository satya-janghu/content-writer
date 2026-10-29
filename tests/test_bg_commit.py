"""
Tests for bg_commit helpers (detached commit + flock-safe TSV append).
"""
from __future__ import annotations

import csv
import os
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import bg_commit  # noqa: E402


def test_commit_in_background_lands(throwaway_git_repo: Path):
    """Detached commit should land in the repo without the caller waiting."""
    f = throwaway_git_repo / "change.txt"
    f.write_text("hello\n", encoding="utf-8")

    proc = bg_commit.commit_in_background(
        message="bg-test commit",
        paths=["change.txt"],
        cwd=str(throwaway_git_repo),
    )
    rc = proc.wait(timeout=10)
    assert rc == 0, "commit subprocess failed"

    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=str(throwaway_git_repo),
        capture_output=True, text=True, check=True,
    ).stdout
    assert "bg-test commit" in log


def test_append_tsv_basic(tmp_path: Path):
    target = tmp_path / "out.tsv"
    bg_commit.append_tsv(str(target), ["a", "b", "c"])
    bg_commit.append_tsv(str(target), ["1", "2", "3"])
    lines = target.read_text(encoding="utf-8").splitlines()
    assert lines == ["a\tb\tc", "1\t2\t3"]


def _child_append(target: str, row_id: int) -> None:
    """Each child appends 20 rows with a marker. Race-test worker."""
    # Child imports bg_commit itself so sys.path handling stays local.
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import bg_commit as bc
    for i in range(20):
        bc.append_tsv(target, [f"w{row_id}", str(i), "row"])


@pytest.mark.race
def test_append_tsv_flock_safe_under_10_writers(tmp_path: Path):
    """10 concurrent processes × 20 rows each = 200 total lines, no interleaved rows."""
    target = str(tmp_path / "race.tsv")
    # Create the file first so children don't race on create().
    Path(target).touch()

    with ProcessPoolExecutor(max_workers=10) as pool:
        list(pool.map(_child_append, [target] * 10, range(10)))

    with open(target, "r", encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f, delimiter="\t"))

    assert len(rows) == 200, f"expected 200 rows, got {len(rows)}"
    # Every row has exactly 3 fields → no interleaving (flock worked).
    bad = [r for r in rows if len(r) != 3]
    assert not bad, f"found {len(bad)} malformed rows — flock didn't hold"
    # All writer IDs represented.
    writer_ids = {r[0] for r in rows}
    assert writer_ids == {f"w{i}" for i in range(10)}


def test_append_tsv_alias_exists():
    """`append_tsv_in_background` is a documented alias; keep it live."""
    assert bg_commit.append_tsv_in_background is bg_commit.append_tsv
