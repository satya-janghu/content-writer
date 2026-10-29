"""
Tests for evaluate.py schema + behavioral sanity.

The deep determinism property (same inputs → same score) is covered by the
score-cache round-trip tests in tests/test_score_cache.py and
`evaluate.py --verify-cache`. This file focuses on fast schema assertions
that don't require GPU model load.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))


def test_suggest_weakest_dim_schema_single():
    """_weakest_dim_report selects by max `(1 - score) * weight` headroom.

    In this fixture every dim except one is at 1.0 (zero headroom), so the
    non-1.0 dim — even with the dim-specific weight — wins by headroom AND
    by raw-min simultaneously. This keeps the schema test tight on the
    shared case; headroom-vs-raw-min divergence is covered by the
    dedicated test below.
    """
    import evaluate
    # scores dict enumerates the 10 scored SERP dims.
    result = {
        "composite": 0.8805,
        "scores": {d: 1.0 for d in evaluate.WEIGHTS},
        "details": {"entity_graph": {"shared_edges": 48}},
    }
    result["scores"]["entity_graph"] = 0.35  # only non-1.0 → weakest by any rule
    report = evaluate._weakest_dim_report("aeo-vs-seo", result)
    assert report["slug"] == "aeo-vs-seo"
    assert report["weakest_dim"] == "entity_graph"
    assert abs(report["weakest_score"] - 0.35) < 1e-6
    assert report["weakest_contrib"] == round(0.35 * evaluate.WEIGHTS["entity_graph"], 4)
    expected_headroom = round((1 - 0.35) * evaluate.WEIGHTS["entity_graph"], 4)
    assert report["weakest_headroom"] == expected_headroom
    assert report["raw_weakest_dim"] == "entity_graph"
    assert abs(report["raw_weakest_score"] - 0.35) < 1e-6
    # Every scored dimension appears in the dims dict.
    assert set(report["dims"].keys()) == set(result["scores"].keys())
    # Each dim has score + weight + contrib + headroom.
    for dim_report in report["dims"].values():
        assert set(dim_report.keys()) == {"score", "weight", "contrib", "headroom"}


def test_weakest_dim_selects_by_headroom_not_raw_min():
    """Headroom selection (decisions #34): a low-weight dim with lower raw score must lose
    to a higher-weight dim with more `(1 - score) * weight` headroom."""
    import evaluate
    # entity_graph (w=0.03) at 0.30 → headroom 0.021
    # corpus_match (w=0.11) at 0.55 → headroom 0.0495
    # topical_coverage (w=0.25) at 0.70 → headroom 0.075 ← should win
    # All others at 1.0.
    scores = {d: 1.0 for d in evaluate.WEIGHTS}
    scores["entity_graph"] = 0.30
    scores["corpus_match"] = 0.55
    scores["topical_coverage"] = 0.70
    result = {"composite": 0.9, "scores": scores, "details": {}}
    report = evaluate._weakest_dim_report("slug", result)
    assert report["weakest_dim"] == "topical_coverage"
    # Raw-min selector would have picked entity_graph; keep it as a secondary
    # diagnostic field so regressions in headroom selection are observable.
    assert report["raw_weakest_dim"] == "entity_graph"


def test_weakest_dim_ignores_ceiling_locked_dims():
    """A dim at 1.0 has zero headroom and must never be selected, even if
    every other dim is tied."""
    import evaluate
    scores = {d: 1.0 for d in evaluate.WEIGHTS}
    # Pick a low-weight dim as the only non-ceiling dim; headroom should
    # still select it despite the tiny weight — anything > 0 beats 0.
    scores["entity_graph"] = 0.99
    result = {"composite": 0.999, "scores": scores, "details": {}}
    report = evaluate._weakest_dim_report("slug", result)
    assert report["weakest_dim"] == "entity_graph"
    assert report["weakest_headroom"] > 0


def test_weakest_dim_tiebreaks_raw_min_then_name():
    """Equal headroom → prefer lower raw score → prefer alphabetical name."""
    import evaluate
    # Craft two dims with identical headroom:
    #   keyword_profile: w=0.05, score=0.60 → headroom 0.02
    #   content_length:  w=0.05, score=0.60 → headroom 0.02
    # All else at 1.0. Tiebreak by score (equal), then by name:
    # "content_length" < "keyword_profile" alphabetically.
    scores = {d: 1.0 for d in evaluate.WEIGHTS}
    scores["keyword_profile"] = 0.60
    scores["content_length"] = 0.60
    result = {"composite": 0.95, "scores": scores, "details": {}}
    report = evaluate._weakest_dim_report("slug", result)
    assert report["weakest_dim"] == "content_length"


def test_suggest_weakest_dim_handles_empty_scores():
    import evaluate
    r = evaluate._weakest_dim_report("x", {"composite": 0.0, "scores": {}, "details": {}})
    assert r["weakest_dim"] is None
    assert r["weakest_score"] is None


def test_weakest_dim_surfaces_sub_scores_when_present():
    """If the weakest dim's detail blob includes `sub_scores`, the report
    surfaces the lowest sub-score name + value. No v3.5 dim currently emits
    sub_scores, but the hook stays wired for future dims that do — and for
    the legacy GEO scorers that are retained in code for possible reuse."""
    import evaluate
    # All other dims at 1.0 → zero headroom → entity_graph is the only
    # reachable target under the headroom selector regardless of weight.
    scores = {d: 1.0 for d in evaluate.WEIGHTS}
    scores["entity_graph"] = 0.3
    result = {
        "composite": 0.9,
        "scores": scores,
        "details": {
            "entity_graph": {
                "sub_scores": {
                    "node_overlap": 0.6,
                    "edge_jaccard": 0.2,   # lowest
                    "density_sim": 0.4,
                }
            }
        },
    }
    report = evaluate._weakest_dim_report("slug", result)
    assert report["weakest_dim"] == "entity_graph"
    assert report["weakest_sub_score"] == {"name": "edge_jaccard", "score": 0.2}


def test_weakest_dim_sub_score_none_when_details_have_no_sub_scores():
    import evaluate
    scores = {d: 1.0 for d in evaluate.WEIGHTS}
    scores["keyword_profile"] = 0.2
    result = {
        "composite": 0.95,
        "scores": scores,
        "details": {"keyword_profile": {"overlap": 0.2}},  # no sub_scores key
    }
    report = evaluate._weakest_dim_report("slug", result)
    assert report["weakest_dim"] == "keyword_profile"
    assert report["weakest_sub_score"] is None


def test_weights_exclude_llm_judge():
    """Post-v3.7: llm_judge is not a scoring dim. All weights are n/82."""
    import evaluate
    assert "llm_judge" not in evaluate.WEIGHTS
    assert abs(sum(evaluate.WEIGHTS.values()) - 1.0) < 1e-9
    expected = {
        "topical_coverage":      25 / 82,
        "keyword_relevance":     10 / 82,
        "corpus_match":          11 / 82,
        "keyword_profile":        5 / 82,
        "entity_coverage":        7 / 82,
        "entity_graph":           3 / 82,
        "narrative_coverage":     3 / 82,
        "readability_structural": 8 / 82,
        "content_length":         5 / 82,
        "banned_words":           5 / 82,
    }
    assert set(evaluate.WEIGHTS.keys()) == set(expected.keys())
    for dim, w in expected.items():
        assert abs(evaluate.WEIGHTS[dim] - w) < 1e-12, (
            f"{dim}: got {evaluate.WEIGHTS[dim]}, expected {w}"
        )


def test_derive_slug():
    import evaluate
    assert evaluate._derive_slug("/path/to/aeo-vs-seo.md") == "aeo-vs-seo"
    assert evaluate._derive_slug("bare.md") == "bare"
    assert evaluate._derive_slug("noext") == "noext"


def test_weights_sum_to_one():
    """Scoring is a weighted sum; weights must sum to 1.0 exactly, or
    composite drifts. The module already asserts this at import time; pin
    it here for CI visibility."""
    import evaluate
    total = sum(evaluate.WEIGHTS.values())
    assert abs(total - 1.0) < 1e-6, f"weights sum to {total}, not 1.0"
