"""
test_gate_integration.py — merge-queue behaviour after removing the cross-
keyword gate (decisions #42).

Proves that drain_merge_queue applies + commits every keeper whose worker
already reported a local-gate pass, without re-regenerating or re-scoring
the other keywords. The safety net shifts from "validate up front" to
"the loop self-corrects through per-keyword iteration."
"""
from __future__ import annotations
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import parallel_loop  # noqa: E402


def _make_keeper(slug: str, uuid: str, delta: float, worktree: Path) -> dict:
    skill = worktree / "rules" / "content-generation" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(f"stub content for {slug}\n")
    return {
        "uuid": uuid,
        "keyword": slug,
        "worktree": str(worktree),
        "baseline": 0.80,
        "pre_composite": 0.82,
        "post_composite": 0.82 + delta,
        "delta_vs_baseline": 0.02 + delta,
        "touched_paths": ["rules/content-generation/SKILL.md"],
        "note": "test keeper",
        "dry_run": True,
    }


def test_merge_queue_applies_every_keeper_without_cross_gate(tmp_path, monkeypatch):
    """Every keeper's diff should be applied + committed in order, with no
    regen-all / score-all side-trip."""
    wt = tmp_path / "worktree"
    wt.mkdir()
    keepers = [
        _make_keeper("aeo-vs-seo", "a" * 8, 0.01, wt),
        _make_keeper("seo-for-saas", "b" * 8, 0.02, wt),
    ]
    monkeypatch.setattr(parallel_loop, "PROJECT_ROOT", tmp_path / "master")
    (tmp_path / "master" / "rules" / "content-generation").mkdir(parents=True)
    baselines = {"aeo-vs-seo": 0.80, "seo-for-saas": 0.80}

    commit_calls: list[tuple[str, list]] = []
    def _fake_commit(msg, paths):
        commit_calls.append((msg, list(paths)))
    def _fake_tsv(path, row):
        pass

    with patch.object(parallel_loop, "bg_commit") as bgc:
        bgc.commit_in_background = _fake_commit
        bgc.append_tsv = _fake_tsv
        commits, committed = parallel_loop.drain_merge_queue(
            keepers, baselines, dry_run=False, iter_no=1,
        )

    assert commits == 2
    assert committed == {"aeo-vs-seo", "seo-for-saas"}
    # Exactly one commit per keeper, in the order they came in.
    seen_slugs = [m[0].split("(")[1].split(",")[0] for m in commit_calls]
    assert seen_slugs == ["aeo-vs-seo", "seo-for-saas"]


def test_merge_queue_dry_run_does_not_commit(tmp_path, monkeypatch):
    wt = tmp_path / "worktree"
    wt.mkdir()
    keepers = [_make_keeper("aeo-vs-seo", "a" * 8, 0.01, wt)]
    monkeypatch.setattr(parallel_loop, "PROJECT_ROOT", tmp_path / "master")
    (tmp_path / "master" / "rules" / "content-generation").mkdir(parents=True)
    baselines = {"aeo-vs-seo": 0.80}

    with patch.object(parallel_loop, "bg_commit") as bgc:
        bgc.commit_in_background = MagicMock()
        bgc.append_tsv = MagicMock()
        commits, committed = parallel_loop.drain_merge_queue(
            keepers, baselines, dry_run=True, iter_no=1,
        )
    assert commits == 0
    assert committed == {"aeo-vs-seo"}  # dry-keep still resets streak
    bgc.commit_in_background.assert_not_called()
