"""P1–P4 scoring-resolution regression tests (decisions.md #35).

Tests focus on the pure-math changes introduced in v3.4-pooling-depth:
  P1  narrative_coverage + keyword_profile: blended (0.6*mean + 0.4*min)
      aggregation across per-competitor scores.
  P2  entity_coverage canonical_overlap: depth-graded `min(1, count/3)` per
      canonical entity (not binary presence).
  P3  entity_graph node_overlap: no more `* 1.5` saturation at 67%.
  P4  corpus_match TF-IDF + BGE legs normalized against
      max(competitor cross-sim), not mean.

The ML-heavy code paths (BGE encode, BART summarize, GLiNER NER) are
monkey-patched so these tests run on CPU without GPU load.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))


# --- P1: mean-min blend helper ----------------------------------------------

def test_mean_min_blend_pure_math():
    import evaluate
    blended, m, lo = evaluate._mean_min_blend([0.9, 0.9, 0.9, 0.4])
    assert abs(m - 0.775) < 1e-9
    assert abs(lo - 0.4) < 1e-9
    assert abs(blended - (0.6 * 0.775 + 0.4 * 0.4)) < 1e-9


def test_mean_min_blend_penalises_concentrated_weakness():
    """Uniform 0.775 vs mean-equivalent mix with one low competitor: blended
    score must be strictly lower for the concentrated-weakness case even
    though pure mean is the same."""
    import evaluate
    uniform_blended, _, _ = evaluate._mean_min_blend([0.775, 0.775, 0.775, 0.775])
    skewed_blended, m2, lo2 = evaluate._mean_min_blend([0.9, 0.9, 0.9, 0.4])
    assert abs(m2 - 0.775) < 1e-9  # same mean
    assert skewed_blended < uniform_blended
    # Gap is 0.4 * (0.775 - 0.4) = 0.15
    assert abs((uniform_blended - skewed_blended) - 0.15) < 1e-9


def test_mean_min_blend_empty_is_zero():
    import evaluate
    b, m, lo = evaluate._mean_min_blend([])
    assert (b, m, lo) == (0.0, 0.0, 0.0)


# --- P1: narrative_coverage uses the blend via sims aggregation -------------

class _FakeEmbed:
    """Encode returns pre-built numpy rows so BGE-cosine math can be tested
    without the real model. Rows are normalised so `a @ b` == cosine."""

    def __init__(self, rows: dict[str, np.ndarray]):
        self._rows = rows

    def encode(self, texts, **_):
        out = []
        for t in texts:
            row = self._rows.get(t)
            if row is None:
                row = np.zeros(4, dtype=np.float32)
            n = np.linalg.norm(row)
            out.append(row / n if n else row)
        return np.asarray(out)


def test_narrative_coverage_penalises_one_weak_competitor(monkeypatch):
    import evaluate
    # Content summary matches 3/4 competitor summaries strongly, one weakly.
    # Under pure mean, the weak one gets averaged away; under the blend it
    # should visibly drag the score down.
    content_summary = "CONTENT"
    summaries = ["COMP_A", "COMP_B", "COMP_C", "COMP_D"]
    e = np.array([1.0, 0, 0, 0], dtype=np.float32)
    a = np.array([0.9, 0.1, 0, 0], dtype=np.float32)  # ~0.994
    b = np.array([0.85, 0.15, 0, 0], dtype=np.float32)
    c = np.array([0.8, 0.2, 0, 0], dtype=np.float32)
    weak = np.array([0.3, 0.9, 0, 0], dtype=np.float32)  # much lower cosine
    fake = _FakeEmbed({content_summary: e, "COMP_A": a, "COMP_B": b,
                       "COMP_C": c, "COMP_D": weak})
    monkeypatch.setattr(evaluate, "get_embed", lambda: fake)

    class _Summarizer:
        def __call__(self, text, **_):
            return [{"summary_text": content_summary}]
    monkeypatch.setattr(evaluate, "get_summarizer", lambda: _Summarizer())

    profile = {"competitor_summaries": summaries}
    # strip_markdown returns the body as-is for plain text; provide something
    # the summarizer will digest.
    body = "Plain body text " * 40  # keep under 800 words so no chunking
    score, detail = evaluate.score_narrative_coverage(body, profile)
    assert 0.0 <= score <= 1.0
    assert "blended_summary_sim" in detail
    assert "min_summary_sim" in detail
    # Blended must be strictly less than the pure mean now that one summary
    # is a visible outlier low.
    assert detail["blended_summary_sim"] < detail["mean_summary_sim"]


# --- P2: entity_coverage canonical_overlap is depth-graded ------------------

def _stub_entity_coverage_helpers(monkeypatch, content_entity_texts=None):
    """Wire up entity_coverage to skip GLiNER + markdown stripping."""
    import evaluate

    class _FakeNER:
        def predict_entities(self, chunk, types, threshold=0.4):
            return [{"text": t} for t in (content_entity_texts or [])]

    monkeypatch.setattr(evaluate, "get_ner", lambda: _FakeNER())
    monkeypatch.setattr(evaluate, "gliner_chunks", lambda text: [text])


def test_canonical_overlap_graded_by_mention_count(monkeypatch):
    import evaluate
    _stub_entity_coverage_helpers(monkeypatch, content_entity_texts=[])

    # Canonical = entities in 2+ competitors. Build a profile so every
    # competitor includes both "alpha" and "beta" (both canonical).
    profile = {
        "competitor_entities": [
            [{"text": "alpha"}, {"text": "beta"}],
            [{"text": "alpha"}, {"text": "beta"}],
            [{"text": "alpha"}, {"text": "beta"}],
        ]
    }

    # One pass-through mention each: each entity's depth = 1/3.
    body_passing = "alpha and beta appear once each."
    score_pass, detail_pass = evaluate.score_entity_coverage(body_passing, profile)
    # detail is rounded to 3 decimals — compare against the rounded target
    assert detail_pass["canonical_overlap"] == pytest.approx(1 / 3, abs=1e-3)
    assert detail_pass["canonical_hits"] == 2

    # Three-plus mentions of one entity saturate at 1.0 for that entity.
    body_deep = "alpha alpha alpha alpha. beta appears once."
    score_deep, detail_deep = evaluate.score_entity_coverage(body_deep, profile)
    assert detail_deep["canonical_overlap"] > detail_pass["canonical_overlap"]
    # alpha fully covered (1.0), beta at 1/3 → mean = (1 + 1/3) / 2 = 0.667
    assert detail_deep["canonical_overlap"] == pytest.approx((1.0 + 1 / 3) / 2, abs=1e-3)

    # Saturation guarantee: extra mentions past 3 don't push beyond 1.0.
    body_saturated = ("alpha " * 30) + ("beta " * 30)
    _, detail_sat = evaluate.score_entity_coverage(body_saturated, profile)
    assert detail_sat["canonical_overlap"] == pytest.approx(1.0)


def test_canonical_overlap_zero_when_no_mentions(monkeypatch):
    import evaluate
    _stub_entity_coverage_helpers(monkeypatch, content_entity_texts=[])
    profile = {
        "competitor_entities": [
            [{"text": "alpha"}, {"text": "beta"}],
            [{"text": "alpha"}, {"text": "beta"}],
        ]
    }
    body = "entirely unrelated content about gardening and weather."
    _, detail = evaluate.score_entity_coverage(body, profile)
    assert detail["canonical_overlap"] == 0.0
    assert detail["canonical_hits"] == 0


# --- P3: entity_graph no longer saturates node_overlap at 67% ---------------

def test_entity_graph_no_node_overlap_saturation_at_two_thirds(monkeypatch):
    """With every competitor node present in content, node_overlap = 1.0.
    Previously the combined formula multiplied it by 1.5 and clamped to 1.0,
    so 67% hit rate already produced the same contribution as 100%. After
    the P3 fix, the node_overlap term contributes exactly 0.5 at full
    coverage and strictly less below that."""
    import evaluate

    # Stub GLiNER + gliner_chunks; content will parse no edges so we use the
    # structural fallback + node_overlap which is all we need for P3.
    class _FakeNER:
        def predict_entities(self, chunk, types, threshold=0.4):
            return []
    monkeypatch.setattr(evaluate, "get_ner", lambda: _FakeNER())
    monkeypatch.setattr(evaluate, "gliner_chunks", lambda t: [t])

    # Competitor graph: 3 nodes, 1 edge so the edges list is non-empty.
    profile_full = {
        "entity_graph": {
            "nodes": [{"entity": "alpha"}, {"entity": "beta"},
                      {"entity": "gamma"}],
            "edges": [{"a": "alpha", "b": "beta", "weight": 1}],
        }
    }
    # Full coverage (all 3 nodes in content, node_overlap = 1.0)
    body_full = "alpha, beta, gamma appear here."
    score_full, d_full = evaluate.score_entity_graph(body_full, profile_full)
    assert d_full["node_overlap"] == pytest.approx(1.0)

    # Two-thirds coverage (node_overlap = 2/3). Under the old `* 1.5`, this
    # would have contributed identically to full coverage. Under the fix,
    # its contribution is strictly less.
    body_partial = "alpha and beta appear, but not the third."
    score_partial, d_partial = evaluate.score_entity_graph(body_partial, profile_full)
    assert d_partial["node_overlap"] == pytest.approx(2 / 3, abs=1e-3)
    # Node-overlap term contribution = 0.5 * node_overlap. Jaccard and
    # density_sim depend on content graph shape so compare after pinning
    # those contributions are identical between the two runs (both have 0
    # content edges).
    assert d_full["content_edges"] == d_partial["content_edges"] == 0
    # Old behaviour: 0.5 * min(1, 0.667*1.5) = 0.5 vs 0.5 * min(1, 1.0*1.5) = 0.5 (TIE).
    # New behaviour: 0.5 * 0.667 = 0.333 vs 0.5 * 1.0 = 0.5 (gap = 0.167).
    # Therefore full must now score strictly higher on node-overlap alone.
    assert score_full > score_partial
    assert (score_full - score_partial) == pytest.approx(0.5 * (1.0 - 2 / 3), abs=1e-6)


# --- P4: corpus_match normalises against max competitor cross-sim -----------

def test_corpus_match_uses_max_not_mean_for_cross_ref(monkeypatch):
    """Feed three competitor docs where cross-similarity is wildly unequal
    (two near-identical, one distant). The ref_sem under the new code must
    equal max of the pairwise cosines, not mean."""
    import evaluate

    # Three corpus docs. Content is orthogonal-ish to all of them so the
    # absolute score isn't what we're testing — we only check that ref_sem
    # and ref_lex report the MAX of the competitor cross-sims, not mean.
    corpus = ["doc one content", "doc two content", "doc three content"]
    content = "entirely different angle here"

    # Emit controlled embeddings so we know the cross-similarities exactly.
    rows = {
        content: np.array([1.0, 0, 0, 0], dtype=np.float32),
        corpus[0]: np.array([0.0, 1.0, 0, 0], dtype=np.float32),
        corpus[1]: np.array([0.0, 0.95, 0.31, 0], dtype=np.float32),  # ≈0.95 with corpus[0]
        corpus[2]: np.array([0.0, 0.2, 0.98, 0], dtype=np.float32),   # much lower cross-sims
    }
    monkeypatch.setattr(evaluate, "get_embed", lambda: _FakeEmbed(rows))

    profile = {
        "competitor_corpus": corpus,
        "top_terms_bm25": [],  # skip the BM25 leg by making it a no-op branch below
    }
    # The BM25 branch falls through to 0.5 when top_terms is empty, which is
    # fine — we're only checking ref_sem_max / ref_lex_max.
    score, detail = evaluate.score_corpus_match(content, profile)

    # Compute expected ref_sem: max over i<j of rows[i]·rows[j] (after
    # unit-normalisation).
    unit = {k: v / np.linalg.norm(v) for k, v in rows.items()}
    pairs = []
    for i in range(len(corpus)):
        for j in range(i + 1, len(corpus)):
            pairs.append(float(unit[corpus[i]] @ unit[corpus[j]]))
    expected_ref_sem = max(pairs)
    expected_mean_ref_sem = float(np.mean(pairs))

    assert detail["ref_sem_max"] == pytest.approx(expected_ref_sem, abs=1e-3)
    # Sanity: the new ref is strictly higher than the old mean would have been.
    assert expected_ref_sem > expected_mean_ref_sem


# --- Guard: no NaN / negative / >1 produced on trivial inputs ---------------

def test_mean_min_blend_clamps_to_nonnegative():
    """Edge case: single-element input — blend == value."""
    import evaluate
    blended, m, lo = evaluate._mean_min_blend([0.5])
    assert blended == m == lo == 0.5
    # Single zero: everything 0, not NaN.
    b0, m0, l0 = evaluate._mean_min_blend([0.0])
    assert b0 == m0 == l0 == 0.0
