"""
Tests for scripts/profile_freshness.py.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))


@pytest.fixture
def stub_profiles(tmp_path: Path, monkeypatch) -> Path:
    """Point PROFILES_DIR at a tmp dir holding one stub profile per configured
    keyword, so freshness checks don't depend on locally built profiles."""
    import profile_freshness
    d = tmp_path / "profiles"
    d.mkdir()
    for p in profile_freshness.expected_profile_paths():
        (d / p.name).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(profile_freshness, "PROFILES_DIR", d)
    return d


def test_hash_stable_across_calls():
    import profile_freshness
    h1 = profile_freshness.compute_competitor_hash()
    h2 = profile_freshness.compute_competitor_hash()
    assert h1 == h2, "hash must be deterministic across calls (no mtime sensitivity)"


def test_hash_changes_when_competitor_edited(tmp_path: Path, monkeypatch):
    import profile_freshness
    # Redirect the competitors dir to a tmp copy
    src = Path(__file__).resolve().parent / "fixtures" / "competitors"
    dst = tmp_path / "competitors"
    import shutil
    shutil.copytree(src, dst)
    monkeypatch.setattr(profile_freshness, "COMPETITORS_DIR", dst)
    monkeypatch.setattr(profile_freshness, "ANALYZER",
                        PROJECT_ROOT / "scripts" / "analyze_competitors.py")

    h_before = profile_freshness.compute_competitor_hash()
    # Touch one competitor's content
    target = next(dst.rglob("*.md"))
    target.write_text(target.read_text(encoding="utf-8") + "\n<!-- test -->\n",
                      encoding="utf-8")
    h_after = profile_freshness.compute_competitor_hash()
    assert h_before != h_after


def test_hash_insensitive_to_pure_mtime_change(tmp_path: Path, monkeypatch):
    """Touch(1) without content change must not invalidate."""
    import profile_freshness, shutil
    src = Path(__file__).resolve().parent / "fixtures" / "competitors"
    dst = tmp_path / "competitors"
    shutil.copytree(src, dst)
    monkeypatch.setattr(profile_freshness, "COMPETITORS_DIR", dst)
    monkeypatch.setattr(profile_freshness, "ANALYZER",
                        PROJECT_ROOT / "scripts" / "analyze_competitors.py")

    h_before = profile_freshness.compute_competitor_hash()
    target = next(dst.rglob("*.md"))
    os.utime(target, (5_000_000, 5_000_000))
    h_after = profile_freshness.compute_competitor_hash()
    assert h_before == h_after, "mtime change without content change should not shift hash"


def test_all_profiles_exist(tmp_path: Path, monkeypatch, stub_profiles):
    import profile_freshness
    # Every configured keyword has a profile -> True.
    assert profile_freshness.all_profiles_exist()

    # Simulate a missing profile by redirecting PROFILES_DIR to an empty dir.
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(profile_freshness, "PROFILES_DIR", empty)
    assert profile_freshness.all_profiles_exist() is False


def test_is_fresh_reflects_stored_vs_current(tmp_path: Path, monkeypatch, tmp_state_file, stub_profiles):
    import profile_freshness
    monkeypatch.setattr(profile_freshness, "STATE_PATH", tmp_state_file)

    fresh, current, stored = profile_freshness.is_fresh()
    assert stored is None
    assert fresh is False  # nothing written yet

    profile_freshness.write_stored_hash(current)
    fresh, _, _ = profile_freshness.is_fresh()
    assert fresh is True


def test_ensure_profiles_fresh_skips_when_fresh(monkeypatch, tmp_state_file, stub_profiles):
    """If hash matches and all profiles present, never shell out to analyze_competitors."""
    import profile_freshness
    monkeypatch.setattr(profile_freshness, "STATE_PATH", tmp_state_file)

    h = profile_freshness.compute_competitor_hash()
    profile_freshness.write_stored_hash(h)

    with patch.object(profile_freshness, "run_analyzer") as run:
        result = profile_freshness.ensure_profiles_fresh(force=False)
        run.assert_not_called()
    assert result["action"] == "skipped"


def test_ensure_profiles_fresh_force_always_rebuilds(monkeypatch, tmp_state_file):
    import profile_freshness
    monkeypatch.setattr(profile_freshness, "STATE_PATH", tmp_state_file)
    h = profile_freshness.compute_competitor_hash()
    profile_freshness.write_stored_hash(h)

    with patch.object(profile_freshness, "run_analyzer", return_value=0) as run:
        result = profile_freshness.ensure_profiles_fresh(force=True)
        run.assert_called_once()
    assert result["action"] == "rebuilt"


def test_ensure_profiles_fresh_propagates_analyzer_failure(monkeypatch, tmp_state_file):
    import profile_freshness
    monkeypatch.setattr(profile_freshness, "STATE_PATH", tmp_state_file)
    # Ensure stale
    profile_freshness.write_stored_hash("deadbeef")

    with patch.object(profile_freshness, "run_analyzer", return_value=1):
        result = profile_freshness.ensure_profiles_fresh(force=False)
    assert result["action"] == "failed"
    assert result.get("returncode") == 1
