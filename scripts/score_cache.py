"""
score_cache.py — per-article composite-score cache.

Stored in `autoresearch-state.json` under `article_scores[slug]`, keyed on:
    - SHA-256 of the content file
    - mtime of the profile file
    - static model_versions dict (changes force a flush)

A cache hit returns the previously-stored full evaluate_content result:
    {"composite": float, "scores": {dim: float}, "details": {dim: {...}}}

Rationale: scoring is a pure function of (content, profile, models). Skill
rules don't affect scores — only generated content does. So edits to
rules/content-generation/SKILL.md do NOT invalidate the cache;
they become effective only when the next regeneration produces new content
with a new SHA, which naturally misses the cache.

The cache is safe to clear at any time (`flush_all()`); scoring will just
re-populate on the next call.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterator

_HERE = Path(__file__).resolve().parent
PROJECT_ROOT = _HERE.parent
STATE_PATH = PROJECT_ROOT / "autoresearch-state.json"

# Bump any entry here when the model or scoring-weights change; all cached
# scores become stale. Only one daemon/evaluate.py reads this at a time; no
# locking needed.
MODEL_VERSIONS: dict[str, str] = {
    "bart": "facebook/bart-large-cnn",
    "bge": "BAAI/bge-large-en-v1.5",
    "reranker": "BAAI/bge-reranker-v2-m3",
    "gliner": "urchade/gliner_large-v2.1",
    "keybert": "keybert-default",
    # If evaluate.py WEIGHTS change, bump this tag to invalidate every cache.
    # v3.3-geo-dims-fixedjudge: corrected the judge model ID from the invalid
    # "gemini-3.0-flash" (which returned 404 on every call) to the actual
    # Gemini API ID "gemini-3-flash-preview". Changing the active judge model
    # is a scoring-semantic change; every cached composite is now stale.
    # v3.4-pooling-depth: P1–P4 scoring-resolution fixes (decisions #35):
    # mean→blended-mean-min aggregation on narrative_coverage + keyword_profile,
    # depth-graded canonical_overlap in entity_coverage, removed the 67% node-
    # overlap saturation in entity_graph, and rescaled corpus_match
    # normalization from competitor-cross-mean to competitor-cross-max on the
    # TF-IDF and BGE legs. Scoring semantics changed across four dims;
    # every cached composite is stale.
    # v3.5-no-geo-scoring: dropped geo_structural_readiness +
    # geo_visibility_sim from the scoring composite (decisions #37). The
    # 0.10 weight they carried was proportionally redistributed across the
    # 11 surviving SERP dims (every weight scales by 1/0.9, using fractions
    # over 90 to keep ratios exact). GEO rule files unchanged — the
    # generation pipeline still applies them in Phase 2. Every cached
    # composite is stale because the denominator and dim set both shifted.
    # v3.6-highest-bar: lifted three benchmarks to the highest-bar principle
    # (decisions #39): entity_coverage canonical set is now the union of any
    # competitor's entities (was: ≥ 2 competitors); per-competitor overlap
    # uses MAX (was: mean × 2 saturated); readability_structural sub-scores
    # use `content / max(competitor)` (was: 0/0.5/0.75/1.0 win-rate);
    # corpus_match BM25 leg ceiling = max(comp_bm25) (was: mean(comp_bm25)),
    # mirroring the v3.4 P4 fix on TF-IDF/BGE legs. Three dims shift
    # downward in expectation; baselines must be re-established.
    # v3.7-no-judge (2026-04-16): removed llm_judge dim (decisions #42).
    # 10 remaining dims, weights rescaled by 90/82 stored as n/82 fractions.
    # Every cached composite is stale (new dim set + new denominator).
    "weights_epoch": "v3.7-no-judge",
}


def _sha256_file(path: str | os.PathLike[str]) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def cache_key(content_path: str | os.PathLike[str],
              profile_path: str | os.PathLike[str]) -> dict:
    """Build a cache key from file state + pinned model versions."""
    return {
        "sha256": _sha256_file(content_path),
        "profile_mtime": int(os.path.getmtime(profile_path)),
        "model_versions": dict(MODEL_VERSIONS),  # copy, not reference
    }


def _lock_path() -> Path:
    """Per-state-file advisory lock. Co-located with the state file so that
    moving STATE_PATH (fixtures, tests) also moves the lock."""
    return STATE_PATH.with_suffix(".json.lock")


@contextlib.contextmanager
def _exclusive_state_lock() -> Iterator[None]:
    """Serialize read-modify-write cycles across processes.

    fcntl.LOCK_EX on a sidecar .json.lock file. Held for the full
    load-mutate-save transaction so concurrent workers never race on the
    rename (a unique temp name per writer avoids FileNotFoundError during
    os.replace when writers race).

    The lock file is created on demand and left on disk — owning-by-inode.
    Equivalent in effect to serializing writes through a single daemon,
    without the RPC layer. See tests/test_score_cache.py::test_concurrent_writers_converge.
    """
    lock = _lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    # open(..., 'a') never truncates, even if the file exists.
    with open(lock, "a") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def _load_state_unlocked() -> dict:
    """Read state.json with no advisory lock. Safe for read-only callers that
    do NOT care about a torn snapshot (cache misses on a torn read just fall
    through to a fresh score)."""
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_state_unlocked(state: dict) -> None:
    """Atomic tmp-rename write. MUST be called under `_exclusive_state_lock()`
    when concurrent writers exist — otherwise two writers can both create
    {STATE_PATH}.tmp and the second's rename fails with FileNotFoundError."""
    tmp = STATE_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, STATE_PATH)


# Public API for other state owners (profile_freshness, parallel_loop) —
# atomic read-modify-write on the shared state.json without them having to
# touch the private helpers directly.
@contextlib.contextmanager
def mutate_state() -> Iterator[dict]:
    """Yield the full state dict under an exclusive lock; the write is
    committed when the block exits without raising.

    Usage:
        with score_cache.mutate_state() as state:
            state["competitor_hash"] = digest
    """
    with _exclusive_state_lock():
        state = _load_state_unlocked()
        yield state
        _save_state_unlocked(state)


def read_state() -> dict:
    """Lock-less snapshot of state.json. A torn read surfaces as an empty
    dict or missing keys, which all callers already treat as miss/default."""
    return _load_state_unlocked()


def get_cached(slug: str, key: dict) -> dict | None:
    """Return the cached result dict for `slug` iff the stored key matches.
    A miss or key mismatch returns None (caller should score and put_cached)."""
    # Reads are lockless — a torn read just becomes a cache miss, which is
    # indistinguishable (to the caller) from any other miss.
    state = _load_state_unlocked()
    entry = state.get("article_scores", {}).get(slug)
    if not entry:
        return None
    stored = entry.get("key")
    if stored != key:
        return None
    return entry.get("result")


def put_cached(slug: str, key: dict, result: dict) -> None:
    """Store `result` under `slug`, replacing any prior entry.
    Lock-protected: concurrent put_cached calls are fully serialized."""
    with _exclusive_state_lock():
        state = _load_state_unlocked()
        state.setdefault("article_scores", {})[slug] = {
            "key": key,
            "result": result,
        }
        _save_state_unlocked(state)


def flush_slug(slug: str) -> bool:
    """Remove one entry. Returns True if anything was removed."""
    with _exclusive_state_lock():
        state = _load_state_unlocked()
        scores = state.get("article_scores", {})
        if slug in scores:
            del scores[slug]
            _save_state_unlocked(state)
            return True
        return False


def flush_all() -> int:
    """Clear the entire article_scores dict. Returns count removed."""
    with _exclusive_state_lock():
        state = _load_state_unlocked()
        scores = state.get("article_scores", {})
        n = len(scores)
        if n:
            state["article_scores"] = {}
            _save_state_unlocked(state)
        return n
