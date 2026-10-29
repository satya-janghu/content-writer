"""
Tests for scripts/score_cache.py — key derivation, round-trip, invalidation,
and the concurrent-writer race the flock fixes.
"""
from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_cache_key_deterministic(tmp_path: Path):
    import score_cache
    c = tmp_path / "c.md"
    p = tmp_path / "p.json"
    _write(c, "content A")
    _write(p, "{}")
    k1 = score_cache.cache_key(c, p)
    k2 = score_cache.cache_key(c, p)
    assert k1 == k2


def test_cache_key_sensitive_to_content(tmp_path: Path):
    import score_cache
    c = tmp_path / "c.md"
    p = tmp_path / "p.json"
    _write(c, "content A")
    _write(p, "{}")
    k1 = score_cache.cache_key(c, p)
    _write(c, "content B")
    k2 = score_cache.cache_key(c, p)
    assert k1 != k2, "sha256 change should shift the cache key"


def test_cache_key_sensitive_to_profile_mtime(tmp_path: Path):
    import score_cache
    c = tmp_path / "c.md"
    p = tmp_path / "p.json"
    _write(c, "content A")
    _write(p, "{}")
    # Force two distinct mtimes
    os.utime(p, (1_000_000, 1_000_000))
    k1 = score_cache.cache_key(c, p)
    os.utime(p, (2_000_000, 2_000_000))
    k2 = score_cache.cache_key(c, p)
    assert k1 != k2


def test_roundtrip_hit(tmp_path: Path, tmp_state_file: Path):
    import score_cache
    c = tmp_path / "c.md"
    p = tmp_path / "p.json"
    _write(c, "x")
    _write(p, "{}")
    k = score_cache.cache_key(c, p)
    result = {"composite": 0.8765, "scores": {"a": 1.0}, "details": {}}
    score_cache.put_cached("slug-a", k, result)
    hit = score_cache.get_cached("slug-a", k)
    assert hit == result


def test_get_returns_none_on_key_mismatch(tmp_path: Path, tmp_state_file: Path):
    import score_cache
    c = tmp_path / "c.md"
    p = tmp_path / "p.json"
    _write(c, "x")
    _write(p, "{}")
    k_original = score_cache.cache_key(c, p)
    score_cache.put_cached("slug-a", k_original, {"composite": 0.5})
    _write(c, "changed")
    k_new = score_cache.cache_key(c, p)
    assert score_cache.get_cached("slug-a", k_new) is None, \
        "content change must invalidate cache"


def test_flush_slug(tmp_path: Path, tmp_state_file: Path):
    import score_cache
    c = tmp_path / "c.md"; _write(c, "x")
    p = tmp_path / "p.json"; _write(p, "{}")
    k = score_cache.cache_key(c, p)
    score_cache.put_cached("slug-a", k, {"composite": 0.5})
    assert score_cache.flush_slug("slug-a") is True
    assert score_cache.get_cached("slug-a", k) is None
    assert score_cache.flush_slug("slug-a") is False  # idempotent


def test_flush_all(tmp_path: Path, tmp_state_file: Path):
    import score_cache
    c = tmp_path / "c.md"; _write(c, "x")
    p = tmp_path / "p.json"; _write(p, "{}")
    k = score_cache.cache_key(c, p)
    for slug in ("a", "b", "c"):
        score_cache.put_cached(slug, k, {"composite": 0.5})
    removed = score_cache.flush_all()
    assert removed == 3
    assert score_cache.flush_all() == 0


def test_model_versions_change_invalidates(tmp_path: Path, tmp_state_file: Path, monkeypatch):
    import score_cache
    c = tmp_path / "c.md"; _write(c, "x")
    p = tmp_path / "p.json"; _write(p, "{}")
    k1 = score_cache.cache_key(c, p)
    score_cache.put_cached("slug-a", k1, {"composite": 0.5})
    # Bump weights_epoch — simulates a future scoring-policy change.
    monkeypatch.setitem(score_cache.MODEL_VERSIONS, "weights_epoch", "v999")
    k2 = score_cache.cache_key(c, p)
    assert score_cache.get_cached("slug-a", k2) is None


# --- Race test: the concurrent-writer race the flock fixes ---

def _child_put(state_path: str, slug: str, n: int) -> None:
    """Worker: repeatedly put_cached for one slug. Each iteration a tiny
    different payload so we can tell who won."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import score_cache
    score_cache.STATE_PATH = Path(state_path)

    # Stable key per process
    c = Path(state_path).parent / f"c-{slug}.md"
    c.write_text("x", encoding="utf-8")
    p = Path(state_path).parent / f"p-{slug}.json"
    p.write_text("{}", encoding="utf-8")

    for i in range(n):
        k = score_cache.cache_key(c, p)
        score_cache.put_cached(slug, k, {"composite": i / 1000.0, "scores": {}, "details": {}})


@pytest.mark.race
def test_concurrent_writers_converge(tmp_path: Path):
    """4 workers × 50 puts each. Expected behaviour:
    - Final state.json parses as valid JSON.
    - Contains exactly 4 slugs.
    - No partial writes, no torn records.

    Acceptance test for the flock-protected read-modify-write in score_cache.
    """
    state = tmp_path / "state.json"
    state.write_text("{}\n", encoding="utf-8")
    with ProcessPoolExecutor(max_workers=4) as pool:
        list(pool.map(
            _child_put,
            [str(state)] * 4,
            ("w0", "w1", "w2", "w3"),
            [50] * 4,
        ))

    # Final assertions
    raw = state.read_text(encoding="utf-8")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        pytest.fail(f"state.json is not valid JSON after concurrent writes: {e}\n---\n{raw[:500]}")

    scores = parsed.get("article_scores", {})
    assert set(scores.keys()) == {"w0", "w1", "w2", "w3"}, (
        f"expected 4 slugs, got {sorted(scores.keys())} — race caused writes to drop"
    )
