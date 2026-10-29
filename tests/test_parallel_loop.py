"""
Tests for scripts/parallel_loop.py — state-file plumbing + baselines.

Batch-mode helpers (rank_keepers, read_proposals_since, create_worktrees,
cleanup_worktrees, _mark_batch_start, _mark_batch_end, _resume_start_iter)
were removed with the async supervisor restructure (decisions #42) so
their coverage is gone; the replacement paths are exercised by
test_async_workers.py + test_gate_integration.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))


# ----- streaks -----

def test_load_streaks_defaults_to_zero(monkeypatch, tmp_state_file: Path):
    import parallel_loop, score_cache
    monkeypatch.setattr(score_cache, "STATE_PATH", tmp_state_file)
    s = parallel_loop._load_streaks(["a", "b", "c"])
    assert s == {"a": 0, "b": 0, "c": 0}


def test_load_streaks_reads_stored(monkeypatch, tmp_state_file: Path):
    import parallel_loop, score_cache
    monkeypatch.setattr(score_cache, "STATE_PATH", tmp_state_file)
    # Write streaks directly through the state file since _mark_batch_end
    # is no longer exposed.
    parallel_loop._write_loop_state({"stagnation_streaks": {"a": 7, "b": 3}})
    s = parallel_loop._load_streaks(["a", "b", "c"])
    assert s == {"a": 7, "b": 3, "c": 0}  # unseen slug starts at 0


# ----- baselines -----

def test_load_baselines_empty_when_not_established(monkeypatch, tmp_state_file: Path):
    """After a fresh reset, baselines are absent. load_baselines() returns {}
    and the main() path refuses to start without them."""
    import parallel_loop, score_cache
    monkeypatch.setattr(score_cache, "STATE_PATH", tmp_state_file)
    assert parallel_loop.load_baselines() == {}


def test_save_and_load_baselines_roundtrip(monkeypatch, tmp_state_file: Path):
    import parallel_loop, score_cache
    monkeypatch.setattr(score_cache, "STATE_PATH", tmp_state_file)
    parallel_loop.save_baselines({"a": 0.85, "b": 0.90})
    loaded = parallel_loop.load_baselines()
    assert loaded == {"a": 0.85, "b": 0.90}
    for v in loaded.values():
        assert isinstance(v, float)


def test_keywords_config_schema(project_root: Path):
    """data/keywords.json must list the expected keyword slugs. When
    baselines are later established they'll be keyed on these slugs."""
    import json
    with open(project_root / "data" / "keywords.json", "r", encoding="utf-8") as f:
        cfg = json.load(f)
    slugs = {kw["slug"] for kw in cfg["keywords"]}
    assert slugs == {"seo-for-roofing", "seo-for-saas", "aeo-vs-seo", "search-intent"}
