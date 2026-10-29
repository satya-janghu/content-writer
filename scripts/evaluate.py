#!/usr/bin/env python3
"""
Evaluate content against a competitive profile (harness v3.7, cache epoch
`v3.7-no-judge`).

10 scoring dimensions; weights are n/82 fractions that sum to 1.0:
  1. Topical coverage (BERTopic clusters + BGE semantic depth)      25/82
  2. Keyword relevance (bge-reranker-v2-m3 per paragraph)           10/82
  3. Corpus match (BM25 + TF-IDF + BGE cosine)                      11/82
  4. Keyword profile (KeyBERT overlap)                               5/82
  5. Entity coverage (GLiNER, union canonical set, depth-graded)     7/82
  6. Entity graph (NetworkX co-occurrence)                           3/82
  7. Narrative coverage (BART summary similarity)                    3/82
  8. Readability + structural (vs best competitor)                   8/82
  9. Content length (policy: hard floor + 7000 cap)                  5/82
 10. Banned words penalty (0.03 per hit)                             5/82

The Gemini LLM judge and the two GEO scorers remain in this module as
dormant code (decisions #37, #42); evaluate_content does not call them.

Usage:
  source .venv/bin/activate
  python scripts/evaluate.py content/seo-for-roofing.md --profile data/profiles/seo-for-roofing-profile.json --verbose
  python scripts/evaluate.py content/ --config data/keywords.json --verbose
"""

import argparse
import concurrent.futures
import json
import os
import re
import sys
import time
from collections import Counter

import numpy as np
import textstat
import networkx as nx
import torch
from sentence_transformers import SentenceTransformer, CrossEncoder
from sklearn.feature_extraction.text import TfidfVectorizer, CountVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from rank_bm25 import BM25Okapi
from gliner import GLiNER
from keybert import KeyBERT
from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BANNED_WORDS_PATH = os.path.join(
    PROJECT_ROOT, "rules", "content-generation", "banned-words.md"
)

# Global lazy-loaded models
_EMBED = None
_RERANKER = None
_NER = None
_KEYBERT = None
_GEMINI_LAST_CALL = 0.0
KEYBERT_VECTORIZER = CountVectorizer(ngram_range=(1, 3), stop_words="english", max_features=2000)
_SUMMARIZER = None
_GEMINI_CLIENT = None


def get_embed():
    global _EMBED
    if _EMBED is None:
        _EMBED = SentenceTransformer(
            "BAAI/bge-large-en-v1.5",
            model_kwargs={"torch_dtype": torch.float16},
            device="cuda",
        )
    return _EMBED


def get_reranker():
    global _RERANKER
    if _RERANKER is None:
        _RERANKER = CrossEncoder(
            "BAAI/bge-reranker-v2-m3",
            automodel_args={"torch_dtype": torch.float16},
            device="cuda",
        )
    return _RERANKER


def get_ner():
    global _NER
    if _NER is None:
        m = GLiNER.from_pretrained("urchade/gliner_large-v2.1", map_location="cuda")
        if hasattr(m, "quantize"):
            try:
                _q = m.quantize(dtype="float16")
                if _q is not None:
                    m = _q
            except Exception as e:
                print(f"  gliner: quantize failed ({e})", file=sys.stderr)
        # Else: skip manual fp16 cast (fp16 deberta-v3 can produce numerical instability).
        if hasattr(m, "compile"):
            try:
                _c = m.compile()
                if _c is not None:
                    m = _c
            except Exception as e:
                print(f"  gliner: compile failed ({e})", file=sys.stderr)
        _NER = m
    return _NER


def get_keybert():
    global _KEYBERT
    if _KEYBERT is None:
        _KEYBERT = KeyBERT(model=get_embed())
    return _KEYBERT


_GLINER_BUDGET = None


def _safe_gliner_budget(types: list[str], hard_cap: int = 384, margin: int = 20) -> int:
    """384-token cap MINUS the entity-prompt prefix that GLiNER prepends.
    See https://github.com/urchade/GLiNER/issues/185 and /issues/231."""
    tok = get_ner().data_processor.transformer_tokenizer
    prompt_text = " ".join(f"<<ENT>> {t} <<SEP>>" for t in types)
    prompt_len = len(tok.encode(prompt_text, add_special_tokens=True))
    return max(64, hard_cap - prompt_len - margin)


def gliner_chunks(text: str, max_tokens_per_chunk: int | None = None) -> list[str]:
    """Token-bounded chunker. Budget = 384 - GLiNER's entity-prompt prefix - safety margin."""
    global _GLINER_BUDGET
    if max_tokens_per_chunk is None:
        if _GLINER_BUDGET is None:
            _GLINER_BUDGET = _safe_gliner_budget(GLINER_ENTITY_TYPES)
            print(f"  gliner: chunk budget = {_GLINER_BUDGET} tokens (after prompt prefix)", file=sys.stderr)
        max_tokens_per_chunk = _GLINER_BUDGET
    tok = get_ner().data_processor.transformer_tokenizer
    sentences = re.split(r'(?<=[.!?])\s+', text)
    chunks: list[str] = []
    buf: list[str] = []
    budget = 0
    for s in sentences:
        ids = tok.encode(s, add_special_tokens=False)
        if len(ids) > max_tokens_per_chunk:
            s = tok.decode(ids[:max_tokens_per_chunk])
            n = max_tokens_per_chunk
        else:
            n = len(ids)
        if buf and budget + n > max_tokens_per_chunk:
            chunks.append(" ".join(buf))
            buf, budget = [s], n
        else:
            buf.append(s)
            budget += n
    if buf:
        chunks.append(" ".join(buf))
    return [c for c in chunks if c.strip()]


def get_summarizer():
    global _SUMMARIZER
    if _SUMMARIZER is None:
        from transformers import pipeline as hf_pipeline
        _SUMMARIZER = hf_pipeline(
            "summarization",
            model="facebook/bart-large-cnn",
            device=0,
            torch_dtype=torch.float16,
        )
        # bart-large-cnn ships with tokenizer.model_max_length = sentinel (1e18),
        # so truncation=True is a silent no-op and chunks >1024 tokens trigger a
        # CUDA position-embedding assert that poisons the whole process context.
        _SUMMARIZER.tokenizer.model_max_length = 1024
    return _SUMMARIZER


# --- Gemini judge configuration ---

# Single source of truth for the model ID used by dim 10 (LLM judge),
# dim 13 (GEO visibility sim), and analyze_competitors.generate_geo_sim_queries.
# NOTE: the correct Gemini API model string is "gemini-3-flash-preview" (no
# dot, -preview suffix). "gemini-3.0-flash" is not a valid ID and returns
# 404 NOT_FOUND. See decisions.md #30.
GEMINI_MODEL = "gemini-3-flash-preview"

# Per-attempt wall-clock ceiling enforced by the ThreadPoolExecutor wrapper
# in _gemini_generate_json. The google-genai SDK's own http_options.timeout
# is NOT reliable — it is overridden to None internally (python-genai #911,
# #1893; pydantic-ai #4031), so we cannot trust the SDK to return within any
# bounded time. 30s is generous for Gemini 3 Flash (typical <5s) while
# keeping 2 attempts × 30s = 60s ceiling per scoring call.
GEMINI_CALL_TIMEOUT_S = 30


def get_gemini():
    """Lazy Gemini client with SDK-native retry for 429/5xx.

    The SDK's `HttpRetryOptions` handles transient errors (quota, server
    hiccups) in-process with exponential backoff. It does NOT cover the
    hang case — that is handled at the wrapper level with a
    ThreadPoolExecutor hard timeout.
    """
    global _GEMINI_CLIENT
    if _GEMINI_CLIENT is None:
        from google import genai
        from google.genai import types as genai_types
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            return None
        _GEMINI_CLIENT = genai.Client(
            api_key=key,
            http_options=genai_types.HttpOptions(
                retry_options=genai_types.HttpRetryOptions(
                    attempts=3,
                    initial_delay=2.0,
                    exp_base=2.0,
                    jitter=0.3,
                    max_delay=20.0,
                    http_status_codes=[429, 500, 502, 503, 504],
                ),
            ),
        )
    return _GEMINI_CLIENT


GLINER_ENTITY_TYPES = [
    "tool", "platform", "software product", "company",
    "search engine", "AI assistant",
    "metric", "KPI", "technical concept",
    "brand", "physical product", "industry body",
    "person", "location"
]


# --- Banned words loader ---

def load_banned_words() -> tuple[list[str], list[str]]:
    phrases = []
    paragraph_starts = []
    if not os.path.exists(BANNED_WORDS_PATH):
        return (["delve", "tapestry", "game-changer"], ["additionally"])

    with open(BANNED_WORDS_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    in_para = False
    in_hard = False
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("## Paragraph-Start Bans"):
            in_para = True
            in_hard = False
            continue
        if stripped.startswith("## Transitional Bans"):
            in_para = False
            in_hard = False
            continue
        if stripped.startswith("## Hard Refusals"):
            in_hard = True
            in_para = False
            continue
        if stripped.startswith("## "):
            in_para = False
            in_hard = False

        if stripped.startswith("- ") and not in_hard:
            phrase = stripped[2:].strip()
            phrase = re.sub(r"\s*\(.*\)\s*$", "", phrase)
            phrase = phrase.split(" / ")[0].strip().lower()
            if phrase:
                if in_para:
                    paragraph_starts.append(phrase)
                else:
                    if phrase not in paragraph_starts:
                        phrases.append(phrase)
    return phrases, paragraph_starts


BANNED_WORDS, PARAGRAPH_START_BANS = load_banned_words()


# --- Utilities ---

def strip_markdown(text: str) -> str:
    text = re.sub(r"!\[.*?\]\(.*?\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    text = re.sub(r"#{1,6}\s+", "", text)
    text = re.sub(r"[*_`~>|]", " ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\[IMAGE:.*?\]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def read_content_file(filepath: str) -> str:
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    parts = content.split("---", 2)
    return parts[2].strip() if len(parts) >= 3 else content.strip()


def extract_sections(body: str, min_words: int = 15) -> list[dict]:
    lines = body.split("\n")
    sections = []
    current_heading = "Introduction"
    current_lines = []
    for line in lines:
        m = re.match(r"^(#{1,6})\s+(.+)$", line)
        if m:
            text = "\n".join(current_lines).strip()
            cleaned = strip_markdown(text)
            if len(cleaned.split()) >= min_words:
                sections.append({"heading": current_heading, "text": cleaned})
            current_heading = m.group(2).strip()
            current_lines = []
        else:
            current_lines.append(line)
    text = "\n".join(current_lines).strip()
    cleaned = strip_markdown(text)
    if len(cleaned.split()) >= min_words:
        sections.append({"heading": current_heading, "text": cleaned})
    return sections


def extract_paragraphs(body: str) -> list[str]:
    return [strip_markdown(p).strip() for p in body.split("\n\n") if len(strip_markdown(p).split()) >= 10]


def tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z][a-z0-9]+", text.lower()) if len(t) >= 2]


def win_rate_score(content_value: float, comp_values: list[float], higher_is_better: bool = True) -> float:
    """How many of the 3 competitors does content beat? Normalize 0/3→0.0, 1/3→0.5, 2/3→0.75, 3/3→1.0."""
    if not comp_values:
        return 0.5
    if higher_is_better:
        wins = sum(1 for v in comp_values if content_value >= v)
    else:
        wins = sum(1 for v in comp_values if content_value <= v)
    return [0.0, 0.5, 0.75, 1.0][min(wins, 3)] if len(comp_values) == 3 else wins / len(comp_values)


# --- Scoring functions ---

def score_topical_coverage(content_body: str, profile: dict) -> tuple[float, dict]:
    """BGE semantic similarity to BERTopic cluster centroids, with depth factor."""
    clusters = profile.get("bertopic_clusters", [])
    if not clusters:
        raise ScoringError("topical_coverage", "profile has no bertopic_clusters; re-run analyze_competitors.py")

    content_sections = extract_sections(content_body)
    if not content_sections:
        cleaned = strip_markdown(content_body)
        if len(cleaned.split()) < 30:
            return (0.0, {"reason": "content too short"})
        content_sections = [{"heading": "Body", "text": cleaned}]

    texts = [s["heading"] + ". " + s["text"] for s in content_sections]
    model = get_embed()
    content_embs = model.encode(texts, show_progress_bar=False, normalize_embeddings=True, batch_size=32)

    covered_weight = 0.0
    total_weight = 0.0
    details = []
    for cluster in clusters:
        w = cluster.get("weight", 0.33)
        total_weight += w
        centroid = np.array(cluster["centroid"])
        sims = content_embs @ centroid
        max_sim = float(sims.max())
        # Depth: mean of top-3 similarities
        top3 = np.sort(sims)[-3:] if len(sims) >= 3 else sims
        depth_sim = float(top3.mean())

        # Fraction: combine max and depth
        if max_sim >= 0.60 and depth_sim >= 0.45:
            frac = 1.0
        elif max_sim >= 0.55:
            # partial based on depth
            frac = 0.5 + 0.5 * min(1.0, max(0.0, (depth_sim - 0.35) / 0.15))
        elif max_sim >= 0.45:
            frac = 0.3 + 0.4 * (max_sim - 0.45) / 0.10
        elif max_sim >= 0.35:
            frac = 0.15 * (max_sim - 0.35) / 0.10
        else:
            frac = 0.0

        covered_weight += w * frac
        details.append({"id": cluster["id"], "w": w, "max": round(max_sim, 3), "depth": round(depth_sim, 3), "frac": round(frac, 3)})

    score = covered_weight / total_weight if total_weight > 0 else 0.0
    return (score, {"clusters": len(clusters), "coverage": round(score, 3)})


def score_keyword_relevance(content_body: str, keyword: str, profile: dict) -> tuple[float, dict]:
    """bge-reranker: score (keyword, each paragraph). Fraction of paragraphs above threshold."""
    paragraphs = extract_paragraphs(content_body)
    if not paragraphs:
        return (0.0, {"reason": "no paragraphs"})

    reranker = get_reranker()
    # Compute reranker scores
    pairs = [(keyword, p) for p in paragraphs]
    scores = reranker.predict(pairs, batch_size=32)
    # bge-reranker scores are raw logits; sigmoid to [0, 1]
    from scipy.special import expit
    probs = expit(scores)

    # Thresholds: strongly relevant >= 0.5, moderately relevant >= 0.3 (calibrated for long-form)
    strong = np.sum(probs >= 0.5)
    moderate = np.sum(probs >= 0.3)
    total = len(probs)
    strong_ratio = strong / total
    moderate_ratio = moderate / total
    # Also include mean_score as a signal (rewards overall relevance even if no paragraph is "strong")
    mean_rel = float(probs.mean())
    combined = 0.4 * strong_ratio + 0.3 * moderate_ratio + 0.3 * mean_rel

    return (min(1.0, combined), {
        "paragraphs": total,
        "strong_relevant": int(strong),
        "moderate_relevant": int(moderate),
        "max_score": float(probs.max()),
        "mean_score": float(probs.mean()),
    })


def score_corpus_match(content_body: str, profile: dict) -> tuple[float, dict]:
    """BM25 (top terms as query) + TF-IDF cosine + BGE semantic cosine."""
    corpus = profile.get("competitor_corpus", [])
    if not corpus or len(corpus) < 2:
        raise ScoringError("corpus_match", f"profile competitor_corpus has {len(corpus) if corpus else 0} entries; need >= 2")

    content_cleaned = strip_markdown(content_body)

    # --- BM25: top terms as query, content as document ---
    top_terms = profile.get("top_terms_bm25", [])
    if top_terms:
        content_tokens = tokenize(content_cleaned)
        competitor_tokens = [tokenize(c) for c in corpus]
        all_docs = competitor_tokens + [content_tokens]
        bm25 = BM25Okapi(all_docs)
        # Query = top terms, split into unigrams
        query = []
        for term in top_terms:
            query.extend(term.split())
        scores = bm25.get_scores(query)
        content_bm25 = float(scores[-1])
        comp_bm25 = scores[:-1]
        # Highest-bar pooling: ceiling = the most BM25-coherent competitor,
        # mirroring the TF-IDF and BGE legs (resolution fix P4, decisions #35). Mean
        # encoded "be as good as the average competitor"; max encodes "match
        # the best competitor on this corpus signal."
        ref_bm25 = float(np.max(comp_bm25)) if len(comp_bm25) else 0.0
        bm25_score = min(1.0, content_bm25 / max(ref_bm25, 0.1)) if ref_bm25 > 0 else min(1.0, content_bm25 / 10.0)
        bm25_score = max(0.0, bm25_score)
    else:
        bm25_score = 0.5

    # --- TF-IDF cosine ---
    vec = TfidfVectorizer(max_features=2000, ngram_range=(1, 2), stop_words="english", min_df=1, sublinear_tf=True)
    try:
        tfidf = vec.fit_transform(corpus + [content_cleaned])
        content_vec = tfidf[-1]
        comp_vecs = tfidf[:-1]
        sims = cosine_similarity(content_vec, comp_vecs)[0]
        mean_lex = float(np.mean(sims))
        comp_cross = []
        for i in range(len(corpus)):
            mask = [j for j in range(len(corpus)) if j != i]
            if mask:
                cross = cosine_similarity(tfidf[i], tfidf[mask])[0]
                comp_cross.append(float(np.mean(cross)))
        # Resolution fix P4: the prior `mean(comp_cross)` encoded the goal as "be as
        # coherent with the corpus as the average competitor". `max(comp_cross)`
        # shifts the ceiling to "match the most corpus-coherent competitor" —
        # a harder target that the scorer can actually differentiate. See
        # decisions.md #35.
        ref_lex = float(np.max(comp_cross)) if comp_cross else 0.3
        lex_score = min(1.0, mean_lex / max(ref_lex, 0.1))
    except Exception:
        lex_score = 0.0
        mean_lex = 0.0

    # --- BGE semantic cosine ---
    model = get_embed()
    content_emb = model.encode([content_cleaned], show_progress_bar=False, normalize_embeddings=True, batch_size=32)[0]
    corpus_embs = model.encode(corpus, show_progress_bar=False, normalize_embeddings=True, batch_size=32)
    sem_sims = corpus_embs @ content_emb
    mean_sem = float(np.mean(sem_sims))
    sem_cross = []
    for i in range(len(corpus)):
        for j in range(i + 1, len(corpus)):
            sem_cross.append(float(corpus_embs[i] @ corpus_embs[j]))
    # Resolution fix P4: max, not mean — see note on TF-IDF leg above.
    ref_sem = float(np.max(sem_cross)) if sem_cross else 0.6
    sem_score = min(1.0, max(0.0, mean_sem / max(ref_sem, 0.1)))

    combined = (bm25_score + lex_score + sem_score) / 3.0
    return (combined, {
        "bm25": round(bm25_score, 3),
        "lexical": round(lex_score, 3),
        "semantic": round(sem_score, 3),
        "mean_lex_sim": round(mean_lex, 3),
        "mean_sem_sim": round(mean_sem, 3),
        "ref_lex_max": round(ref_lex, 3),
        "ref_sem_max": round(ref_sem, 3),
    })


def _mean_min_blend(values) -> tuple[float, float, float]:
    """Combined mean+worst aggregator for per-competitor scores (resolution fix P1).

    Returns `(blended, mean, min)` where `blended = 0.6 * mean + 0.4 * min`.
    Pure mean hides concentrated weakness against a single competitor: a piece
    that matches 3/4 competitors at 0.9 and one at 0.4 averages the same as
    one at 0.775 across the board — but a generative engine inspecting all
    four treats the missing angle as a gap. The 0.6/0.4 split rewards breadth
    while still penalising the worst-case gap. See decisions.md #35.
    """
    arr = np.asarray(values, dtype=float)
    if arr.size == 0:
        return (0.0, 0.0, 0.0)
    m = float(arr.mean())
    lo = float(arr.min())
    return (0.6 * m + 0.4 * lo, m, lo)


def score_keyword_profile(content_body: str, profile: dict) -> tuple[float, dict]:
    """Semantic similarity between content KeyBERT terms and competitor KeyBERT terms."""
    competitor_kb = profile.get("competitor_keybert", [])
    if not competitor_kb:
        raise ScoringError("keyword_profile", "profile has no competitor_keybert; re-run analyze_competitors.py")

    cleaned = strip_markdown(content_body)
    kb = get_keybert()
    try:
        content_kws = kb.extract_keywords(
            cleaned,
            vectorizer=KEYBERT_VECTORIZER,
            top_n=20,
            use_mmr=True,
            diversity=0.5,
        )
        content_terms = [kw for kw, _ in content_kws]
    except Exception as e:
        raise ScoringError("keyword_profile", f"KeyBERT error: {e}") from e

    if not content_terms:
        return (0.0, {"reason": "no content keywords"})

    model = get_embed()
    content_embs = model.encode(content_terms, show_progress_bar=False, normalize_embeddings=True, batch_size=32)

    # For each competitor: embed its top-20 terms, compute best-match similarity per content term
    per_comp_scores = []
    for comp_kws in competitor_kb:
        comp_terms = [item["term"] for item in comp_kws]
        if not comp_terms:
            per_comp_scores.append(0.0)
            continue
        comp_embs = model.encode(comp_terms, show_progress_bar=False, normalize_embeddings=True, batch_size=32)
        sim_matrix = content_embs @ comp_embs.T  # [content_n, comp_n]
        # For each competitor term, best content match (rewards coverage of competitor vocabulary)
        best_for_comp = sim_matrix.max(axis=0)
        per_comp_scores.append(float(best_for_comp.mean()))

    # Resolution fix P1: pure mean hid concentrated weakness; blend with min to penalise
    # a single-competitor miss. See decisions.md #35.
    blended, mean_score, min_score = _mean_min_blend(per_comp_scores)
    # Map cosine [0.4, 0.85] → [0, 1]
    final = min(1.0, max(0.0, (blended - 0.4) / 0.45))
    return (final, {
        "content_terms": len(content_terms),
        "mean_best_match": round(mean_score, 3),
        "min_best_match": round(min_score, 3),
        "blended_best_match": round(blended, 3),
    })


def score_entity_coverage(content_body: str, profile: dict) -> tuple[float, dict]:
    """GLiNER entity overlap with competitor entity sets + per-competitor win-rate."""
    competitor_entities = profile.get("competitor_entities", [])
    if not competitor_entities:
        raise ScoringError("entity_coverage", "profile has no competitor_entities; re-run analyze_competitors.py")

    cleaned = strip_markdown(content_body)
    ner = get_ner()
    # Chunked extraction
    chunks = gliner_chunks(cleaned)
    content_entities_raw = []
    for chunk in chunks:
        if not chunk.strip():
            continue
        ents = ner.predict_entities(chunk, GLINER_ENTITY_TYPES, threshold=0.4)
        content_entities_raw.extend(ents)

    content_texts = {e["text"].strip().lower() for e in content_entities_raw}

    # Highest-bar canonical set: every entity mentioned by ANY competitor is
    # in scope. The prior `≥ 2 competitors` filter encoded an
    # intersection-leaning "domain-expected" criterion that let real
    # competitive entities (mentioned by one competitor) drop out. The
    # autoresearch principle for coverage-type dims is union — the article
    # must cover everything any competitor covers. `entity_counts` is kept as
    # a diagnostic so we can still see which entities were corroborated by
    # multiple competitors.
    entity_counts = Counter()
    for comp_ents in competitor_entities:
        for e in comp_ents:
            entity_counts[e["text"].lower()] += 1
    canonical = {ent for ent in entity_counts if ent}
    canonical_2plus = sum(1 for cnt in entity_counts.values() if cnt >= 2)

    # Per-canonical-entity depth: `min(1.0, mentions / 3)` per entity, then
    # averaged. A pass-through mention → 0.33, deep treatment (3+) → 1.0.
    # Resolution fix P2 (decisions #35) — replaces the prior binary substring presence.
    content_raw_lower = strip_markdown(content_body).lower()
    if canonical:
        per_entity_depth = [
            min(1.0, content_raw_lower.count(ent) / 3.0)
            for ent in canonical if ent
        ]
        canonical_overlap = float(np.mean(per_entity_depth)) if per_entity_depth else 0.0
        canonical_hits = sum(1 for d in per_entity_depth if d > 0)
    else:
        canonical_overlap = 0.5
        canonical_hits = 0

    # Per-competitor coverage ratio. Highest-bar aggregation: take the MAX —
    # match the most entity-rich competitor's coverage rate. Mean diluted
    # against weaker competitors and let the article skate by on the easiest
    # one. The detail payload exposes mean and min for diagnostics.
    overlaps = []
    for comp_ents in competitor_entities:
        comp_texts = {e["text"].lower() for e in comp_ents}
        if not comp_texts:
            overlaps.append(0.0)
            continue
        common = content_texts & comp_texts
        overlaps.append(len(common) / len(comp_texts))
    max_overlap = float(np.max(overlaps)) if overlaps else 0.0
    mean_overlap = float(np.mean(overlaps)) if overlaps else 0.0

    content_count = len(content_texts)
    comp_counts = [len({e["text"].lower() for e in c}) for c in competitor_entities]
    win = win_rate_score(content_count, comp_counts, higher_is_better=True)

    # Combined: canonical-union depth (primary, every competitor entity in scope)
    # + max per-competitor overlap (secondary, match the most entity-rich
    # competitor) + win-rate on entity count.
    score = 0.60 * canonical_overlap + 0.25 * max_overlap + 0.15 * win
    return (score, {
        "content_entities": content_count,
        "canonical_count": len(canonical),
        "canonical_2plus": canonical_2plus,
        "canonical_overlap": round(canonical_overlap, 3),
        "canonical_hits": canonical_hits,
        "max_overlap": round(max_overlap, 3),
        "mean_overlap": round(mean_overlap, 3),
        "win_rate": round(win, 3),
    })


def score_entity_graph(content_body: str, profile: dict) -> tuple[float, dict]:
    """Weighted Jaccard similarity of entity co-occurrence edges (content vs competitor union)."""
    competitor_graph = profile.get("entity_graph", {})
    comp_edges = competitor_graph.get("edges", [])
    if not comp_edges:
        raise ScoringError("entity_graph", "profile entity_graph.edges is empty; re-run analyze_competitors.py")

    # Build content graph: use GLiNER to get entities, then check co-occurrence within sections
    cleaned = strip_markdown(content_body)
    ner = get_ner()
    chunks = gliner_chunks(cleaned)
    content_ents_raw = []
    for chunk in chunks:
        if not chunk.strip():
            continue
        content_ents_raw.extend(ner.predict_entities(chunk, GLINER_ENTITY_TYPES, threshold=0.4))
    content_entity_texts = list({e["text"].strip().lower() for e in content_ents_raw})

    # Co-occurrence within sections
    G = nx.Graph()
    for sec in extract_sections(content_body):
        text_lower = sec["text"].lower()
        present = [et for et in content_entity_texts if et in text_lower]
        for i, a in enumerate(present):
            for b in present[i + 1:]:
                if G.has_edge(a, b):
                    G[a][b]["weight"] += 1
                else:
                    G.add_edge(a, b, weight=1)
    content_edges = {(min(u, v), max(u, v)): d["weight"] for u, v, d in G.edges(data=True)}

    # Competitor edges
    comp_edge_dict = {(min(e["a"], e["b"]), max(e["a"], e["b"])): e["weight"] for e in comp_edges}

    # Node overlap: check via substring in raw content (more forgiving than exact GLiNER match)
    comp_nodes = {node["entity"] for node in competitor_graph.get("nodes", [])}
    content_lower = strip_markdown(content_body).lower()
    nodes_present = sum(1 for n in comp_nodes if n in content_lower)
    node_overlap = nodes_present / max(len(comp_nodes), 1)

    # Primary: weighted Jaccard on edges (exact entity text match)
    all_pairs = set(content_edges.keys()) | set(comp_edge_dict.keys())
    shared = set(content_edges.keys()) & set(comp_edge_dict.keys())
    if not all_pairs:
        raise ScoringError("entity_graph", "no entity edges in content or profile — content likely has fewer than 2 entities in any paragraph")
    mins = 0.0
    maxs = 0.0
    for pair in all_pairs:
        c = content_edges.get(pair, 0)
        k = comp_edge_dict.get(pair, 0)
        mins += min(c, k)
        maxs += max(c, k)
    jaccard = mins / maxs if maxs > 0 else 0.0

    # Secondary: structural similarity fallback (node density, edge density ratios)
    # This gives partial credit even when exact text matches fail
    n_content_nodes = G.number_of_nodes()
    n_comp_nodes = len({node["entity"] for node in competitor_graph.get("nodes", [])})
    n_content_edges = len(content_edges)
    n_comp_edges = len(comp_edge_dict)

    # Density ratio: how close is content's graph density to competitor's?
    def density(n, e):
        return e / max(n * (n - 1) / 2, 1) if n > 1 else 0.0

    content_density = density(n_content_nodes, n_content_edges)
    comp_density = density(n_comp_nodes, n_comp_edges)
    density_sim = 1.0 - abs(content_density - comp_density) / max(content_density, comp_density, 0.01)
    density_sim = max(0.0, min(1.0, density_sim))

    # Combined: node_overlap (primary: does content mention the same entities) 50%
    #           + Jaccard edge overlap (exact co-occurrence match) 30%
    #           + density_sim (graph shape) 20%
    # Resolution fix P3: the prior `min(1.0, node_overlap * 1.5)` saturated the node-
    # overlap term at a 67% hit rate, capping the dim's ceiling with zero reward
    # for exceeding two-thirds of the competitor node set. node_overlap is
    # already in [0, 1]; use it directly. See decisions.md #35.
    score = 0.5 * node_overlap + 0.3 * jaccard + 0.2 * density_sim
    return (score, {
        "content_edges": n_content_edges,
        "competitor_edges": n_comp_edges,
        "shared_edges": len(shared),
        "node_overlap": round(node_overlap, 3),
        "jaccard": round(jaccard, 3),
        "density_sim": round(density_sim, 3),
    })


def score_narrative_coverage(content_body: str, profile: dict) -> tuple[float, dict]:
    """BART summary of content; BGE cosine to each competitor summary; average."""
    comp_summaries = profile.get("competitor_summaries", [])
    if not comp_summaries:
        raise ScoringError("narrative_coverage", "profile has no competitor_summaries; re-run analyze_competitors.py")

    cleaned = strip_markdown(content_body)
    try:
        summarizer = get_summarizer()
        # BART input limit
        words = cleaned.split()
        if len(words) > 800:
            chunks = [" ".join(words[i:i + 800]) for i in range(0, len(words), 800)]
            chunk_summaries = []
            for c in chunks[:6]:
                try:
                    out = summarizer(c, max_length=100, min_length=40, do_sample=False, truncation=True)
                    chunk_summaries.append(out[0]["summary_text"])
                except Exception:
                    chunk_summaries.append(c[:200])
            combined = " ".join(chunk_summaries)
            if len(combined.split()) > 300:
                out = summarizer(combined, max_length=180, min_length=80, do_sample=False, truncation=True)
                content_summary = out[0]["summary_text"]
            else:
                content_summary = combined
        else:
            out = summarizer(cleaned, max_length=180, min_length=80, do_sample=False, truncation=True)
            content_summary = out[0]["summary_text"]
    except Exception as e:
        raise ScoringError("narrative_coverage", f"BART failed: {e}") from e

    model = get_embed()
    embs = model.encode([content_summary] + comp_summaries, show_progress_bar=False, normalize_embeddings=True, batch_size=32)
    content_emb = embs[0]
    comp_embs = embs[1:]
    sims = comp_embs @ content_emb
    # Resolution fix P1: blend mean with min so a single-summary miss visibly drops the
    # score instead of being averaged away. See decisions.md #35.
    blended_sim, mean_sim, min_sim = _mean_min_blend(sims)
    # Map cosine [0.3, 0.85] to [0, 1]
    score = min(1.0, max(0.0, (blended_sim - 0.3) / 0.55))
    return (score, {
        "mean_summary_sim": round(mean_sim, 3),
        "min_summary_sim": round(min_sim, 3),
        "blended_summary_sim": round(blended_sim, 3),
    })


def score_readability_structural(content_body: str, profile: dict) -> tuple[float, dict]:
    """Per-competitor win-rate across readability + structural metrics."""
    cleaned = strip_markdown(content_body)
    per_comp = profile.get("per_competitor_detail", [])
    if not per_comp:
        raise ScoringError("readability_structural", "profile has no per_competitor_detail; re-run analyze_competitors.py")

    # Readability (aim: within range; win = content's metric is within range of any competitor's)
    comp_fre = [c["readability"]["flesch_reading_ease"] for c in per_comp]
    comp_fog = [c["readability"]["gunning_fog"] for c in per_comp]
    comp_dale = [c["readability"]["dale_chall"] for c in per_comp]

    content_fre = textstat.flesch_reading_ease(cleaned)
    content_fog = textstat.gunning_fog(cleaned)
    content_dale = textstat.dale_chall_readability_score(cleaned)

    # For FRE: higher is more readable; aim to be within competitive band (not extreme)
    fre_floor, fre_tgt = min(comp_fre), max(comp_fre)
    fog_floor, fog_tgt = min(comp_fog), max(comp_fog)
    dale_floor, dale_tgt = min(comp_dale), max(comp_dale)

    def band_score(val, floor, target, tol=8.0):
        if floor <= val <= target:
            return 1.0
        d = min(abs(val - floor), abs(val - target))
        return max(0.0, 1.0 - d / tol)

    read_score = (band_score(content_fre, fre_floor, fre_tgt, 15.0) +
                  band_score(content_fog, fog_floor, fog_tgt, 4.0) +
                  band_score(content_dale, dale_floor, dale_tgt, 2.0)) / 3.0

    # Structural: heading count, list item count, answer-block ratio (per-competitor win)
    lines = content_body.split("\n")
    content_headings = len([l for l in lines if re.match(r"^#{1,6}\s", l)])
    content_lists = len(re.findall(r"^[-*]\s", content_body, re.MULTILINE))
    content_answer_ratio = 0.0
    total_h2 = 0
    answer_blocks = 0
    for sec in content_body.split("\n##")[1:]:
        total_h2 += 1
        parts = sec.split("\n", 1)
        if len(parts) < 2:
            continue
        first_para = parts[1].lstrip().split("\n\n", 1)[0]
        wc = len(strip_markdown(first_para).split())
        if 40 <= wc <= 100:
            answer_blocks += 1
    content_answer_ratio = answer_blocks / total_h2 if total_h2 > 0 else 0.0

    comp_headings = [c["structural"]["heading_count"] for c in per_comp]
    comp_answer = [c["structural"]["answer_block_ratio"] for c in per_comp]

    # Highest-bar structural pooling (realign 2026-04-15): match the BEST
    # competitor on each structural metric. Win-rate let an article ahead of
    # 2/3 competitors saturate at 0.75 even when the third had 2× the heading
    # count. Now `content / max(comp_X)` (clipped to [0, 1]) — full credit
    # only when the article reaches the top competitor's level.
    def _floor_target(content_val: float, comp_vals: list[float]) -> float:
        cap = max(comp_vals) if comp_vals else 0.0
        if cap <= 0:
            return 1.0
        return float(min(1.0, content_val / cap))

    h_score = _floor_target(content_headings, comp_headings)
    a_score = _floor_target(content_answer_ratio, comp_answer)

    struct_score = 0.5 * h_score + 0.5 * a_score

    combined = 0.5 * read_score + 0.5 * struct_score
    return (combined, {
        "fre": round(content_fre, 1),
        "fog": round(content_fog, 1),
        "dale": round(content_dale, 1),
        "readability": round(read_score, 3),
        "content_headings": content_headings,
        "max_comp_headings": max(comp_headings) if comp_headings else 0,
        "content_answer_ratio": round(content_answer_ratio, 3),
        "max_comp_answer_ratio": round(max(comp_answer), 3) if comp_answer else 0.0,
        "heading_score": round(h_score, 3),
        "answer_block_score": round(a_score, 3),
    })


def score_content_length(content_body: str, profile: dict) -> tuple[float, dict]:
    policy = profile.get("content_length_policy", {})
    hard_min = policy.get("hard_min", 500)
    soft_min = policy.get("soft_min", 1000)
    soft_max = policy.get("soft_max", 5000)
    hard_max = policy.get("hard_max", 7000)

    wc = len(strip_markdown(content_body).split())

    if wc >= hard_max:
        score = max(0.0, 0.3 - (wc - hard_max) / 1000)
    elif wc > soft_max:
        score = 1.0 - 0.3 * (wc - soft_max) / max(hard_max - soft_max, 1)
    elif wc >= soft_min:
        score = 1.0
    elif wc >= hard_min:
        score = 0.5 + 0.5 * (wc - hard_min) / max(soft_min - hard_min, 1)
    else:
        score = max(0.0, wc / hard_min) * 0.5

    return (score, {"word_count": wc, "hard_min": hard_min, "soft_max": soft_max, "hard_max": hard_max})


class ScoringError(RuntimeError):
    """A dimension scorer could not produce a valid score and refuses to fabricate one.

    Raised instead of returning a neutral 0.5 on failure. Callers (daemon →
    client → iter_worker) treat this as "drop the iteration", never as a
    quality signal. See decisions.md #33.
    """

    def __init__(self, dim: str, reason: str):
        self.dim = dim
        self.reason = reason
        super().__init__(f"[{dim}] {reason}")


def _gemini_generate_json(prompt: str, log_tag: str = "gemini") -> dict | list:
    """Call the Gemini judge in JSON mode with a hard wall-clock timeout.

    Returns the parsed JSON object/array on success. Raises `ScoringError` if
    all retries are exhausted — callers must NOT substitute a neutral fallback;
    the loop layer handles the failure by dropping the iteration (decisions
    #33).

    No longer called by the scoring composite (decisions #42). Retained for
    possible reinstatement; still referenced by the dormant
    score_llm_judge + score_geo_visibility_simulation functions and by
    analyze_competitors.generate_geo_sim_queries.

    **Why a ThreadPoolExecutor wrapper and not `http_options.timeout`:**
    the google-genai SDK does not honour http_options.timeout (it passes
    timeout=None to httpx), so requests can hang indefinitely at the
    socket layer (python-genai #911, #1893; pydantic-ai #4031). The only
    reliable way to enforce a wall-clock timeout on generate_content is to
    run it in a worker thread and call future.result(timeout=...). On
    timeout we shut the executor down with wait=False; the underlying
    thread may linger until the OS drops the TCP connection (seconds to
    minutes), but it does not block the main flow.

    Inner 429/5xx retries are handled by the SDK's HttpRetryOptions (see
    get_gemini()). Our outer loop handles: (a) hang → hard timeout, (b)
    non-JSON response → parse retry, (c) terminal exceptions. 404 (wrong
    model ID) is terminal and short-circuits the retries.
    """
    client = get_gemini()
    if client is None:
        raise ScoringError("gemini", "GEMINI_API_KEY not set")

    # Pace requests for free-tier ~60 req/min ceiling.
    global _GEMINI_LAST_CALL
    elapsed = time.time() - _GEMINI_LAST_CALL
    if elapsed < 1.05:
        time.sleep(1.05 - elapsed)

    from slog import slog as _slog
    last_err: str = ""
    GEMINI_OUTER_ATTEMPTS = 3
    call_t0 = time.time()
    for attempt in range(GEMINI_OUTER_ATTEMPTS):
        raw = None
        attempt_t0 = time.time()
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(
            client.models.generate_content,
            model=GEMINI_MODEL,
            contents=prompt,
            config={
                "temperature": 0.0,
                "response_mime_type": "application/json",
            },
        )
        try:
            resp = future.result(timeout=GEMINI_CALL_TIMEOUT_S)
            _GEMINI_LAST_CALL = time.time()
            raw = resp.text
            parsed = json.loads(raw)
            _slog("gemini", log_tag, "CALL_OK",
                  attempt=attempt + 1, elapsed=time.time() - attempt_t0,
                  total_elapsed=time.time() - call_t0)
            return parsed
        except concurrent.futures.TimeoutError:
            last_err = f"wall-clock timeout after {GEMINI_CALL_TIMEOUT_S}s"
            _slog("gemini", log_tag, "CALL_TIMEOUT",
                  attempt=attempt + 1, timeout_s=GEMINI_CALL_TIMEOUT_S,
                  elapsed=time.time() - attempt_t0)
            print(f"  {log_tag}: hard timeout (attempt {attempt+1}/{GEMINI_OUTER_ATTEMPTS}) — thread leaks until socket drops", file=sys.stderr)
            executor.shutdown(wait=False, cancel_futures=True)
        except json.JSONDecodeError as e:
            last_err = str(e)
            snippet = (raw or "<empty>")[:120]
            _slog("gemini", log_tag, "CALL_JSON_ERROR",
                  attempt=attempt + 1, elapsed=time.time() - attempt_t0,
                  snippet=snippet[:80])
            print(f"  {log_tag}: non-JSON response (attempt {attempt+1}/{GEMINI_OUTER_ATTEMPTS}): {snippet}", file=sys.stderr)
            executor.shutdown(wait=False, cancel_futures=True)
            time.sleep(3)
        except Exception as e:
            last_err = str(e)[:200]
            msg = str(e)
            is_quota = "429" in msg or "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower()
            is_404 = "404" in msg or "NOT_FOUND" in msg
            tag = "quota" if is_quota else ("404" if is_404 else "error")
            _slog("gemini", log_tag, f"CALL_{tag.upper()}",
                  attempt=attempt + 1, elapsed=time.time() - attempt_t0,
                  error=last_err[:120])
            print(f"  {log_tag}: {tag} (attempt {attempt+1}/{GEMINI_OUTER_ATTEMPTS}): {last_err[:160]}", file=sys.stderr)
            executor.shutdown(wait=False, cancel_futures=True)
            if is_404:
                raise ScoringError("gemini", f"Gemini 404 (terminal): {last_err[:150]}") from e
            time.sleep(3 * (3 ** attempt))
            _GEMINI_LAST_CALL = time.time()

    _slog("gemini", log_tag, "CALL_EXHAUSTED",
          attempts=GEMINI_OUTER_ATTEMPTS, total_elapsed=time.time() - call_t0,
          last_error=last_err[:120])
    raise ScoringError("gemini", f"Gemini failed after {GEMINI_OUTER_ATTEMPTS} attempts: {last_err[:150]}")


def score_llm_judge(content_body: str, profile: dict) -> tuple[float, dict]:
    """Gemini 3.0 Flash rates factual accuracy, coherence, originality vs competitor excerpts."""
    cleaned = strip_markdown(content_body)
    # Truncate to ~6000 words for prompt budget
    words = cleaned.split()
    if len(words) > 5500:
        cleaned = " ".join(words[:5500])

    comp_summaries = profile.get("competitor_summaries", [])
    comp_excerpts = "\n\n---\n\n".join(
        [f"[Competitor {i+1} summary]\n{s}" for i, s in enumerate(comp_summaries)]
    )

    keyword = profile.get("keyword", "")

    prompt = f"""You are a strict SEO content quality reviewer. Rate the following article on three dimensions (0-100 each):

1. factual_accuracy: Are claims, statistics, tool names, and technical details correct and verifiable? Penalize invented statistics and wrong information.
2. coherence: Does the article flow logically? Do sections connect? Is the argument structure clear?
3. originality: Does the article offer insight beyond rephrasing competitor content? Or is it generic filler?

Reference competitor summaries (for comparison on originality):
{comp_excerpts}

--- ARTICLE TO RATE ---
Target keyword: "{keyword}"

{cleaned}

--- END ARTICLE ---

Output ONLY valid JSON with this schema, no other text:
{{"factual_accuracy": <0-100>, "coherence": <0-100>, "originality": <0-100>, "note": "<one sentence>"}}"""

    # _gemini_generate_json raises ScoringError on failure; we let that
    # propagate (decisions #33). No more silent 0.5 fallback.
    data = _gemini_generate_json(prompt, log_tag="llm_judge")

    fa = float(data.get("factual_accuracy", 50)) / 100.0
    co = float(data.get("coherence", 50)) / 100.0
    og = float(data.get("originality", 50)) / 100.0
    composite = (fa + co + og) / 3.0
    return (composite, {
        "factual": round(fa, 3),
        "coherence": round(co, 3),
        "originality": round(og, 3),
        "note": data.get("note", "")[:200],
    })


def score_banned_words(content_body: str) -> tuple[float, dict]:
    cleaned = strip_markdown(content_body).lower()
    hits = 0
    hit_list = []
    for phrase in BANNED_WORDS:
        if " " in phrase:
            count = cleaned.count(phrase)
        else:
            count = len(re.findall(r"\b" + re.escape(phrase) + r"\b", cleaned))
        if count > 0:
            hits += count
            hit_list.append((phrase, count))
    for line in content_body.split("\n"):
        stripped = line.strip().lower()
        for ban in PARAGRAPH_START_BANS:
            if re.match(r"^" + re.escape(ban) + r"\b", stripped):
                hits += 1
                hit_list.append((f"[p-start] {ban}", 1))
                break
    return (max(0.0, 1.0 - hits * 0.03), {"hits": hits, "top_hits": hit_list[:10]})


# --- GEO structural parsing helpers ---

# Regex catalogue for statistics_density (dim 12 sub-score 6). Each pattern
# matches a concrete numerical claim worth citing. Kept conservative: generic
# numeric tokens without units are NOT counted because they inflate the score
# on tables of contents and list enumerations.
_STATS_PATTERNS = [
    re.compile(r"\b\d+\.?\d*\s*%"),                                       # percentages
    re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?"),                               # currency
    re.compile(r"\b\d+\.?\d*\s*(?:million|billion|trillion|thousand|M|B|K)\b", re.IGNORECASE),
    re.compile(r"\b(?:19|20)\d{2}\b"),                                    # years 1900-2099
    re.compile(r"\b\d+\.?\d*\s*x\b", re.IGNORECASE),                      # 3x, 10.5x
    re.compile(r"\b\d+\.?\d*\s*(?:seconds?|minutes?|hours?|days?|weeks?|months?|years?)\b", re.IGNORECASE),
    re.compile(r"\b\d{2,}[\d,]*\s+(?:users?|companies|customers?|sites?|pages?|queries|visitors?|leads?|clicks?)\b", re.IGNORECASE),
]

# Sentences whose surface form signals "assertion backed by a source/study/stat".
# GEO-SFE finds this structure is the most citable content unit.
_CLAIM_EVIDENCE_CUES = re.compile(
    r"\b(?:according to|studies? (?:show|find|suggest)s?|research (?:shows?|finds?|suggests?|indicates?)|"
    r"data (?:shows?|suggests?|indicates?)|analysis (?:shows?|finds?)|"
    r"a (?:\d{4}|recent) study|report(?:s|ed)? (?:by|from)|survey(?:s|ed)? (?:by|of|from)|"
    r"found (?:that|in)|evidence suggests?|analysis of)\b",
    re.IGNORECASE,
)


def _count_headings_by_level(body: str) -> dict[int, int]:
    """{level: count} for #..###### headings. Skips fenced code blocks."""
    counts: dict[int, int] = {}
    in_fence = False
    for line in body.split("\n"):
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = re.match(r"^(#{1,6})\s+\S", line)
        if m:
            lvl = len(m.group(1))
            counts[lvl] = counts.get(lvl, 0) + 1
    return counts


def _section_lengths_by_level(body: str, level: int) -> list[int]:
    """Word counts of each section opened by a heading at the given level.

    Uses strip_markdown on each section's prose so counts are prose-only
    (excludes heading text itself, list markup, table pipes, etc.).
    """
    pattern = re.compile(rf"^#{{{level}}}\s+.+$", re.MULTILINE)
    matches = list(pattern.finditer(body))
    if not matches:
        return []
    lengths: list[int] = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        section_text = body[start:end]
        # Count only the prose directly under this heading — stop at the first
        # sub-heading of ANY level, so nested H3/H4 content isn't double-counted
        # inside its parent's length.
        cut = re.search(r"^#{1,6}\s+", section_text, re.MULTILINE)
        if cut:
            section_text = section_text[: cut.start()]
        cleaned = strip_markdown(section_text)
        lengths.append(len(cleaned.split()))
    return lengths


def _geo_paragraphs(body: str) -> list[str]:
    """Prose paragraphs only. Excludes headings, tables, list-only blocks, fenced code."""
    paragraphs: list[str] = []
    # strip fenced code before splitting
    stripped = re.sub(r"```.*?```", "", body, flags=re.DOTALL)
    for raw in re.split(r"\n\s*\n", stripped):
        chunk = raw.strip()
        if not chunk:
            continue
        lines = [ln for ln in chunk.split("\n") if ln.strip()]
        if not lines:
            continue
        # Skip heading-only blocks
        if all(re.match(r"^#{1,6}\s+", ln) for ln in lines):
            continue
        # Skip list-only blocks
        if all(re.match(r"^(?:[-*+]|\d+\.)\s+", ln.lstrip()) for ln in lines):
            continue
        # Skip table-only blocks (all lines start with |)
        if all(ln.lstrip().startswith("|") for ln in lines):
            continue
        cleaned = strip_markdown(chunk).strip()
        if len(cleaned.split()) >= 5:
            paragraphs.append(cleaned)
    return paragraphs


def _count_structured_element_words(body: str) -> int:
    """Words contained inside tables, ordered+unordered lists, and definition-style
    blocks (``**Label:** ...`` inline-header bullets). Counts use stripped text.
    """
    total = 0
    stripped = re.sub(r"```.*?```", "", body, flags=re.DOTALL)
    # Tables: any line starting with `|` (excluding separator rows that are just |---|---|)
    for line in stripped.split("\n"):
        t = line.strip()
        if t.startswith("|") and not re.match(r"^\|[\s:\-|]+\|?\s*$", t):
            total += len(strip_markdown(t).split())
    # Lists (ordered + unordered)
    for line in stripped.split("\n"):
        t = line.lstrip()
        if re.match(r"^(?:[-*+]|\d+\.)\s+", t):
            total += len(strip_markdown(t).split())
    return total


def _count_emphasis_words(body: str) -> int:
    """Words inside bold (**x**, __x__) or italic (*x*, _x_) markers.

    A word is counted at most once even if it is both bold and italic
    (*** nesting), because the dim tracks emphasis density, not token
    cardinality.
    """
    stripped = re.sub(r"```.*?```", "", body, flags=re.DOTALL)
    # Match bold first (longer markers), then remaining italic.
    emph_spans: list[str] = []
    for m in re.finditer(r"\*\*([^*\n]{1,200})\*\*|__([^_\n]{1,200})__", stripped):
        emph_spans.append(m.group(1) or m.group(2) or "")
    # remove the bold regions so italic doesn't double-count
    no_bold = re.sub(r"\*\*[^*\n]{1,200}\*\*|__[^_\n]{1,200}__", " ", stripped)
    for m in re.finditer(r"(?<![\*\w])\*([^*\n]{1,200})\*(?![\*\w])|(?<![_\w])_([^_\n]{1,200})_(?![_\w])", no_bold):
        emph_spans.append(m.group(1) or m.group(2) or "")
    words = 0
    for span in emph_spans:
        words += len(re.findall(r"\w+", span))
    return words


def _count_statistics(text: str) -> int:
    total = 0
    for pat in _STATS_PATTERNS:
        total += len(pat.findall(text))
    return total


def _count_claim_evidence_sentences(text: str) -> int:
    """Sentences matching a claim-with-evidence surface pattern (cue phrase only).

    Narrower than _count_evidence_backed_sentences below; used for diagnostic
    reporting in dim 12 measurements.
    """
    sentences = re.split(r"(?<=[.!?])\s+", text)
    hits = 0
    for s in sentences:
        if _CLAIM_EVIDENCE_CUES.search(s):
            hits += 1
    return hits


def _count_major_claim_sentences(text: str) -> int:
    """Declarative sentences ≥5 words long. Heuristic for "major claims"
    eligible for evidence backing in dim 12 sub-score 7."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    total = 0
    for s in sentences:
        s = s.strip()
        if not s or s.endswith("?"):  # questions aren't claims
            continue
        if len(s.split()) < 5:  # too short to be a substantive claim
            continue
        total += 1
    return total


def _count_evidence_backed_sentences(text: str) -> int:
    """Sentences that either cite a source (_CLAIM_EVIDENCE_CUES) OR contain a
    concrete numerical claim (_STATS_PATTERNS). A statistic stands on its own
    as evidence even without a "according to" cue."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    hits = 0
    for s in sentences:
        if _CLAIM_EVIDENCE_CUES.search(s):
            hits += 1
            continue
        if any(p.search(s) for p in _STATS_PATTERNS):
            hits += 1
    return hits


def _band_score(value: float, lo: float, hi: float, decay: float) -> float:
    """1.0 inside [lo, hi], linear decay outside with slope 1/decay. Clamped to [0, 1]."""
    if lo <= value <= hi:
        return 1.0
    d = min(abs(value - lo), abs(value - hi))
    return max(0.0, 1.0 - d / decay)


def _floor_target_score(value: float, saturation_target: float) -> float:
    """Asymmetric band: linear ramp from 0 at value=0 to 1.0 at value=saturation_target,
    then saturates at 1.0 for any value above the target.

    Used for absolute GEO sub-scores where "more is always at least as good"
    (statistics density, claim-evidence ratio). Unlike _band_score, there is
    no upper-side decay — content that exceeds the target isn't penalised,
    since the research shows these are quality floors, not precision bands.
    """
    if saturation_target <= 0:
        return 0.0
    if value >= saturation_target:
        return 1.0
    return max(0.0, value / saturation_target)


def score_geo_structural_readiness(content_body: str, profile: dict) -> tuple[float, dict]:
    """Deterministic structural-readiness score for generative-engine citation.

    Grounded in GEO-SFE (Yu et al. 2026) structural findings: heading depth,
    section balance, paragraph length, structured-element share, emphasis
    density, and the statistics-density + claim-evidence findings from
    Aggarwal KDD'24 replicated by Lee (2026). Seven equally-weighted
    sub-scores averaged to a composite in [0, 1]. No GPU dependency.
    """
    cleaned = strip_markdown(content_body)
    total_words = max(1, len(cleaned.split()))

    # Sub-score 1: heading depth (target 3-5 levels used, decay 2 steps)
    levels_used = sorted(_count_headings_by_level(content_body).keys())
    depth = len(levels_used)
    heading_depth = _band_score(depth, 3, 5, 2.0)

    # Sub-score 2: section balance — mean CV of section lengths across H2 & H3
    balance_scores: list[float] = []
    for lvl in (2, 3):
        lens = _section_lengths_by_level(content_body, lvl)
        if len(lens) >= 2:
            mean = sum(lens) / len(lens)
            if mean > 0:
                var = sum((x - mean) ** 2 for x in lens) / len(lens)
                cv = (var ** 0.5) / mean
                balance_scores.append(max(0.0, 1.0 - cv))
    section_balance = sum(balance_scores) / len(balance_scores) if balance_scores else 0.5

    # Sub-score 3: mean paragraph length (target 150-300 words)
    paragraphs = _geo_paragraphs(content_body)
    mean_para_wc = (sum(len(p.split()) for p in paragraphs) / len(paragraphs)) if paragraphs else 0.0
    paragraph_length = _band_score(mean_para_wc, 150.0, 300.0, 150.0)

    # Sub-score 4: structured-element ratio (target 5-15% of total words)
    struct_words = _count_structured_element_words(content_body)
    struct_ratio = struct_words / total_words
    structured_ratio = _band_score(struct_ratio, 0.05, 0.15, 0.10)

    # Sub-score 5: emphasis density (target 5-10% of total words)
    emph_words = _count_emphasis_words(content_body)
    emph_ratio = emph_words / total_words
    emphasis_density = _band_score(emph_ratio, 0.05, 0.10, 0.07)

    # Sub-score 6: statistics density — ABSOLUTE research-derived target.
    # Aggarwal KDD'24 found statistics addition was the single most effective
    # GEO strategy; Lee 2026 replicated on every production AI platform. The
    # band [3, 5] concrete data points per 1000 words is the quality floor.
    # NOT normalized against competitors — competitors are SERP-ranked, not
    # GEO-ranked, and often contain zero statistics (thin SERP content).
    our_stats = _count_statistics(cleaned)
    our_stats_per_1k = our_stats * 1000 / total_words
    # Saturation at 4/1k (midpoint of [3, 5]). Articles above 4/1k get 1.0;
    # articles with fewer stats ramp linearly from 0 to 1.
    statistics_density = _floor_target_score(our_stats_per_1k, 4.0)

    # Sub-score 7: claim–evidence ratio — ABSOLUTE target band [0.4, 0.6].
    # Ratio of evidence-backed sentences (cite a source OR contain a concrete
    # statistic) to all "major claim" sentences (≥5 words, declarative). Yu
    # et al. (GEO-SFE) identified claim→evidence→implication as the most
    # citable content unit. 40-60% of major claims carrying explicit evidence
    # is the research-derived target.
    major_claims = _count_major_claim_sentences(cleaned)
    evidence_backed = _count_evidence_backed_sentences(cleaned)
    if major_claims == 0:
        ce_ratio = 0.0
    else:
        ce_ratio = evidence_backed / major_claims
    # Saturation at 0.5 (midpoint of [0.4, 0.6]). Articles above 0.5 of
    # major claims evidence-backed get 1.0; articles below ramp linearly.
    claim_evidence = _floor_target_score(ce_ratio, 0.5)

    sub_scores = {
        "heading_depth": heading_depth,
        "section_balance": section_balance,
        "paragraph_length": paragraph_length,
        "structured_ratio": structured_ratio,
        "emphasis_density": emphasis_density,
        "statistics_density": statistics_density,
        "claim_evidence": claim_evidence,
    }
    composite = sum(sub_scores.values()) / len(sub_scores)

    return (composite, {
        "sub_scores": {k: round(v, 3) for k, v in sub_scores.items()},
        "measurements": {
            "heading_levels_used": depth,
            "mean_paragraph_words": round(mean_para_wc, 1),
            "paragraph_count": len(paragraphs),
            "structured_word_ratio": round(struct_ratio, 3),
            "emphasis_word_ratio": round(emph_ratio, 3),
            "stats_per_1k_words": round(our_stats_per_1k, 2),
            "major_claim_count": major_claims,
            "evidence_backed_count": evidence_backed,
            "claim_evidence_ratio": round(ce_ratio, 3),
        },
        "targets": {
            # Research-derived absolute targets. GEO dims do not use
            # competitor data for normalization — SERP competitors have no
            # validated GEO merit (Chen et al. 2025, SERP↔GEO ρ ≈ 0).
            "heading_depth": "3-5 levels (GEO-SFE)",
            "mean_paragraph_words": "150-300 words (GEO-SFE)",
            "structured_word_ratio": "5-15% of content (GEO-SFE)",
            "emphasis_word_ratio": "5-10% of content (GEO-SFE)",
            "stats_per_1k_words": "3-5 per 1000 (Aggarwal KDD'24 + Lee 2026)",
            "claim_evidence_ratio": "0.4-0.6 of major claims (GEO-SFE)",
        },
    })


# Position rotation across queries gives us deterministic position-bias
# coverage without randomizing per-call (which would defeat the score cache).
def _query_source_layout(query_idx: int, n_competitors: int) -> tuple[int, list[int]]:
    """(our_position, competitor_positions) for a given query index.

    All sources are indexed 0..n_competitors (inclusive of our article). Our
    article rotates through positions 0, 1, 2... across queries; competitors
    fill the remaining slots in their natural order. Rotation is modulo the
    total source count so rotation stays deterministic for any query count.
    """
    total = n_competitors + 1
    our_pos = query_idx % total
    comp_positions = [i for i in range(total) if i != our_pos]
    return our_pos, comp_positions


def _parse_citations(response: str) -> list[int]:
    """Return 1-indexed source numbers cited, in order of first appearance.

    Handles "Source 1", "[Source 1]", "(Source 1)", "[1]". Deduplicates.
    """
    cites: list[int] = []
    seen: set[int] = set()
    for m in re.finditer(r"\bSource\s+(\d+)\b|\[(\d+)\]", response, flags=re.IGNORECASE):
        n = int(m.group(1) or m.group(2))
        if n not in seen:
            seen.add(n)
            cites.append(n)
    return cites


def _our_citation_position(response: str, our_source_number: int) -> int | None:
    """Sentence index (0-based) of the first citation to our source, or None."""
    sentences = re.split(r"(?<=[.!?])\s+", response)
    for i, sent in enumerate(sentences):
        for m in re.finditer(r"\bSource\s+(\d+)\b|\[(\d+)\]", sent, flags=re.IGNORECASE):
            if int(m.group(1) or m.group(2)) == our_source_number:
                return i
    return None


def score_geo_visibility_simulation(content_body: str, profile: dict) -> tuple[float, dict]:
    """Simulates generative-engine citation of our content against competitors.

    For each query in profile["geo_sim_queries"], asks Gemini to answer using
    only labelled sources (our article + 3 competitor summaries). Parses the
    response for citations to our source number and aggregates three signals:

      - citation_presence (weight 0.4): was our source cited at all?
      - position_adjusted  (weight 0.3): sentence-position of first citation,
                                          linear decay 1.0 -> 0.5
      - content_influence  (weight 0.3): unigram-overlap proxy for how much
                                          of the response came from our text

    Averaged across queries. Gemini failures fall back to neutral 0.5 for
    that query; if all queries fail, returns (0.5, {"reason": ...}) — the
    per-keyword gate still rejects any proposal that regresses a baseline.
    """
    queries = profile.get("geo_sim_queries") or []
    competitor_summaries = profile.get("competitor_summaries") or []
    if not queries:
        raise ScoringError("geo_visibility_sim", "profile missing geo_sim_queries; re-run analyze_competitors.py")
    if len(competitor_summaries) < 1:
        raise ScoringError("geo_visibility_sim", "profile has no competitor_summaries")

    # Budget: our article must fit alongside 3 summaries in the prompt. Trim
    # to ~4000 words (same spirit as score_llm_judge).
    cleaned = strip_markdown(content_body)
    our_words = cleaned.split()
    if len(our_words) > 4000:
        cleaned = " ".join(our_words[:4000])
    our_token_set = {w.lower() for w in re.findall(r"\w{4,}", cleaned)}

    per_query: list[dict] = []
    for q_idx, query in enumerate(queries):
        our_pos, comp_positions = _query_source_layout(q_idx, len(competitor_summaries))
        sources: list[str] = [""] * (len(competitor_summaries) + 1)
        sources[our_pos] = cleaned
        for comp_i, pos in enumerate(comp_positions):
            if comp_i < len(competitor_summaries):
                sources[pos] = competitor_summaries[comp_i]
        source_block = "\n\n".join(
            f"[Source {i+1}]\n{src}" for i, src in enumerate(sources)
        )

        prompt = f"""You are a generative search engine. Answer the user's question using ONLY the provided sources. Cite sources inline using the format [Source N]. Do not invent information beyond what the sources say. Provide a comprehensive answer.

User query: "{query}"

{source_block}

Output ONLY valid JSON with this schema:
{{"answer": "<your full answer with [Source N] citations>", "citations_used": [<list of source numbers cited>]}}"""

        # Let ScoringError propagate — if ANY query fails, the whole scoring
        # fails. No per-query neutral fallback (decisions #33); the 3x outer
        # retry in _gemini_generate_json is the only allowed resilience.
        data = _gemini_generate_json(prompt, log_tag=f"geo_vis[q{q_idx+1}]")

        answer = str(data.get("answer", ""))
        our_source_number = our_pos + 1  # 1-indexed in the prompt
        cites = _parse_citations(answer)
        cited = our_source_number in cites

        # 1) Citation presence
        presence = 1.0 if cited else 0.0

        # 2) Position-adjusted: sentence index of first our-citation; earlier = higher
        pos_sent = _our_citation_position(answer, our_source_number)
        sentence_count = max(1, len(re.split(r"(?<=[.!?])\s+", answer)))
        if pos_sent is None:
            position_adj = 0.0
        else:
            # Linear decay 1.0 -> 0.5 from sentence 0 to sentence N-1
            position_adj = 1.0 - 0.5 * (pos_sent / max(1, sentence_count - 1))

        # 3) Content influence: unigram-overlap proxy for how much of the
        #    response came from our text vs. competitor text — but only
        #    meaningful when we're actually cited. An uncited response that
        #    shares common words with our article shouldn't register as
        #    "influenced by" us; citation is the traffic-bearing mechanism.
        if not cited:
            influence = 0.0
        else:
            resp_tokens = [w.lower() for w in re.findall(r"\w{4,}", answer)]
            if not resp_tokens:
                influence = 0.0
            else:
                resp_set = set(resp_tokens)
                our_overlap = len(resp_set & our_token_set) / max(1, len(resp_set))
                comp_overlaps: list[float] = []
                for summary in competitor_summaries:
                    c_set = {w.lower() for w in re.findall(r"\w{4,}", summary)}
                    if c_set:
                        comp_overlaps.append(len(resp_set & c_set) / max(1, len(resp_set)))
                comp_mean_overlap = sum(comp_overlaps) / len(comp_overlaps) if comp_overlaps else 0.0
                # Normalize: our_overlap / (our_overlap + comp_mean_overlap). 1.0 if
                # we dominate, 0.5 if tied, 0.0 if competitors dominate entirely.
                denom = our_overlap + comp_mean_overlap
                influence = our_overlap / denom if denom > 0 else 0.0

        query_score = 0.4 * presence + 0.3 * position_adj + 0.3 * influence
        per_query.append({
            "query": query[:80],
            "score": round(query_score, 3),
            "cited": cited,
            "our_position_in_sources": our_source_number,
            "first_cite_sentence": pos_sent,
            "presence": round(presence, 3),
            "position_adj": round(position_adj, 3),
            "influence": round(influence, 3),
            "citations_in_response": cites,
        })

    # Aggregate — all queries must have succeeded by this point (any failure
    # in the loop above raised ScoringError). Post-decisions-#33, per-query
    # neutral is not a thing, so every per_query entry has a real score.
    assert per_query, "unreachable: loop must have produced >=1 entry"
    scores_list = [q["score"] for q in per_query]
    composite = sum(scores_list) / len(scores_list)
    return (composite, {
        "per_query": per_query,
        "queries_scored": len(scores_list),
        "queries_total": len(per_query),
    })


# --- Composite ---

# v3.5: GEO dims removed from scoring composite (decisions #37). The scorer
# no longer benchmarks generative-engine structural readiness or citation
# simulation — we have no SERP-ranked reference content validating those
# targets, and the autoresearch loop cannot ratchet against them productively
# (see decisions #36 for the compliance-gap pattern that made the attempt
# infeasible). The GEO rules stay in the GEO section of
# rules/content-generation/SKILL.md and still shape generation.
#
# v3.7-no-judge (2026-04-16): removed dim 10 (llm_judge). The Gemini judge
# was contributing 8/90 weight and ~5-7s wait per scoring call, and was the
# main source of stochastic scoring noise (±0.005 composite drift). Removing
# it both cuts per-score wall-clock and tightens the merge-queue gate. The
# remaining 10 SERP dims absorb the freed 8/90 proportionally; every weight
# is rescaled by 90/82 so the composite still sums to 1.0. Fractions over 82
# keep ratios exact. score_llm_judge + _gemini_generate_json + GEMINI_MODEL
# stay in this file — reinstatement = put one `run(…)` line back and bump
# the epoch. See decisions #42.
WEIGHTS = {
    "topical_coverage":       25 / 82,
    "keyword_relevance":      10 / 82,
    "corpus_match":           11 / 82,
    "keyword_profile":         5 / 82,
    "entity_coverage":         7 / 82,
    "entity_graph":            3 / 82,
    "narrative_coverage":      3 / 82,
    "readability_structural":  8 / 82,
    "content_length":          5 / 82,
    "banned_words":            5 / 82,
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9, f"weights sum to {sum(WEIGHTS.values())}"


def evaluate_content(content_path: str, profile: dict, verbose: bool = False) -> dict:
    """Score one article against one profile.

    Returns: {"composite": float, "scores": {dim: float}, "details": {dim: {...}},
              "_dim_timings": {dim: float}}
    All values are JSON-serializable so the daemon can return them directly.
    """
    from slog import slog as _slog
    slug = os.path.splitext(os.path.basename(content_path))[0]
    body = read_content_file(content_path)
    keyword = profile.get("keyword", "")

    t0 = time.time()
    scores = {}
    details = {}
    dim_timings = {}

    def run(name, fn, *args):
        nonlocal scores, details, dim_timings
        dt0 = time.time()
        s, d = fn(*args)
        dt = time.time() - dt0
        scores[name] = s
        details[name] = d
        dim_timings[name] = round(dt, 3)
        _slog("evaluate", slug, "SCORE_DIM",
              dim=name, score=s, elapsed=dt, weight=WEIGHTS.get(name, 0))
        if verbose:
            print(f"    {name:22s}: {s:.3f} (w={WEIGHTS[name]:.2f}, contrib={s*WEIGHTS[name]:.3f}, {dt:.1f}s)", file=sys.stderr)

    if verbose:
        print(f"\n  Scoring: {os.path.basename(content_path)}", file=sys.stderr)

    run("topical_coverage", score_topical_coverage, body, profile)
    run("keyword_relevance", score_keyword_relevance, body, keyword, profile)
    run("corpus_match", score_corpus_match, body, profile)
    run("keyword_profile", score_keyword_profile, body, profile)
    run("entity_coverage", score_entity_coverage, body, profile)
    run("entity_graph", score_entity_graph, body, profile)
    run("narrative_coverage", score_narrative_coverage, body, profile)
    run("readability_structural", score_readability_structural, body, profile)
    run("content_length", score_content_length, body, profile)
    run("banned_words", score_banned_words, body)

    composite = sum(scores[d] * WEIGHTS[d] for d in scores)
    elapsed = time.time() - t0
    _slog("evaluate", slug, "SCORE_COMPLETE",
          composite=composite, elapsed=elapsed, dims_scored=len(scores))

    if verbose:
        print(f"    {'COMPOSITE':22s}: {composite:.4f} (scored in {elapsed:.1f}s)", file=sys.stderr)
        for dim in ["entity_coverage", "entity_graph", "narrative_coverage", "keyword_relevance", "corpus_match"]:
            if dim in details:
                print(f"    [{dim}] {details[dim]}", file=sys.stderr)

    result = {"composite": composite, "scores": scores, "details": details,
              "_dim_timings": dim_timings}
    return result


def _score_one(content_path: str, profile_path: str, verbose: bool, use_daemon: bool,
               slug: str | None = None, use_cache: bool = True) -> dict:
    """Score one article, preferring the daemon when it's up.

    Returns: {"composite": float, "scores": {...}, "details": {...}}
    Falls back to in-process scoring if the daemon is unreachable or refuses.
    In-process mode bypasses the cache entirely (the cache lives in the daemon).
    """
    if use_daemon:
        try:
            from eval_client import score_article as _score_via_daemon
            return _score_via_daemon(
                content_path, profile_path, verbose=verbose,
                slug=slug, use_cache=use_cache,
            )
        except ImportError:
            pass  # client module missing; fall through to in-process
        except Exception as e:
            # DaemonUnavailable or any transport error → silently fall back
            if verbose:
                print(f"  [daemon unreachable: {e}; falling back to in-process]", file=sys.stderr)
    # in-process (no cache)
    with open(profile_path, "r", encoding="utf-8") as f:
        profile = json.load(f)
    return evaluate_content(content_path, profile, verbose=verbose)


def _derive_slug(content_path: str) -> str:
    """slug = basename without .md extension. Matches data/profiles/{slug}-profile.json."""
    base = os.path.basename(content_path)
    return base[:-3] if base.endswith(".md") else base


def _weakest_dim_report(slug: str, result: dict) -> dict:
    """Compact JSON for the loop's hypothesize step.

    Selects by max composite headroom `(1 - score) * weight`, tie-broken by
    lowest raw score. Raw-min selection (the prior behaviour) pointed the
    proposer at low-weight dims where even a perfect fix barely moved the
    composite; headroom selection targets the dim with the most realizable
    composite gain. Ceiling-locked dims (score = 1.0) have zero headroom and
    are never targeted. See docs/decisions.md #34.
    """
    scores = result.get("scores", {}) or {}
    details = result.get("details", {}) or {}
    if not scores:
        return {"slug": slug, "composite": result.get("composite"),
                "weakest_dim": None, "weakest_score": None,
                "dims": {}, "note": "scores unavailable"}

    def _headroom(d: str) -> float:
        return (1.0 - float(scores[d])) * float(WEIGHTS.get(d, 0.0))

    # Primary: max headroom. Tiebreak: lowest raw score, then alphabetical
    # dim name for determinism. Implemented as `min` over a negated-headroom
    # tuple so the name tiebreaker reads in the natural direction.
    weakest_dim = min(
        scores,
        key=lambda d: (-_headroom(d), float(scores[d]), d),
    )
    # Secondary diagnostic: raw-min selection (what the prior selector picked).
    # Kept so iteration logs can compare the two and surface cases where a
    # ceiling-locked dim was dragging selection.
    raw_weakest_dim = min(scores, key=lambda d: (float(scores[d]), d))

    # Contribution = score × weight; headroom = (1-score) × weight. Both are
    # surfaced per-dim so the proposer can reason about current impact AND
    # unrealized potential.
    dims = {
        d: {
            "score": round(float(scores[d]), 4),
            "weight": WEIGHTS.get(d, 0.0),
            "contrib": round(float(scores[d]) * WEIGHTS.get(d, 0.0), 4),
            "headroom": round(_headroom(d), 4),
        } for d in scores
    }
    weakest_detail = details.get(weakest_dim, {})
    # For composite-subscored dims that expose a `sub_scores` block, surface
    # the lowest sub-score so the proposer targets the right sub-signal rather
    # than rewriting at the dim granularity. No v3.5 scored dim currently
    # emits sub_scores, but the hook is preserved for future dims that do.
    sub = weakest_detail.get("sub_scores") if isinstance(weakest_detail, dict) else None
    weakest_sub = None
    if isinstance(sub, dict) and sub:
        weakest_sub_name = min(sub, key=lambda k: sub[k])
        weakest_sub = {"name": weakest_sub_name, "score": round(float(sub[weakest_sub_name]), 4)}
    return {
        "slug": slug,
        "composite": round(float(result.get("composite", 0.0)), 4),
        "weakest_dim": weakest_dim,
        "weakest_score": round(float(scores[weakest_dim]), 4),
        "weakest_contrib": round(float(scores[weakest_dim]) * WEIGHTS.get(weakest_dim, 0.0), 4),
        "weakest_headroom": round(_headroom(weakest_dim), 4),
        "weakest_detail": weakest_detail,
        "weakest_sub_score": weakest_sub,
        "raw_weakest_dim": raw_weakest_dim,
        "raw_weakest_score": round(float(scores[raw_weakest_dim]), 4),
        "dims": dims,
        # Full per-dim measurement detail. Surfaced so downstream consumers
        # (e.g. the propose prompt) can show the agent what the scorer
        # actually saw for every dim, not just the auto-selected weakest one.
        # The `weakest_dim`/`weakest_*` fields are kept as legacy diagnostics;
        # the propose path no longer pre-targets a single dim (decisions #38).
        "details": details,
    }


def _verify_cache_one(slug: str, content_path: str, profile_path: str, verbose: bool) -> tuple[bool, float, float]:
    """Round-trip test: flush, score (miss → store), score again (hit), compare.

    Returns (ok, miss_score, hit_score). Bitwise equality is expected because
    the cache is pure retrieval — the hit path does not recompute anything.
    """
    try:
        from eval_client import flush_cache, score_article, DaemonUnavailable
    except ImportError:
        raise RuntimeError("verify-cache requires the eval_server daemon; eval_client missing")

    try:
        flush_cache(slug)
    except DaemonUnavailable as e:
        raise RuntimeError(f"verify-cache requires the eval_server daemon; daemon unavailable: {e}")

    r1 = score_article(content_path, profile_path, verbose=verbose, slug=slug, use_cache=True)
    r2 = score_article(content_path, profile_path, verbose=verbose, slug=slug, use_cache=True)

    if r1.get("_cache") != "miss":
        raise RuntimeError(f"expected cache miss on first call, got _cache={r1.get('_cache')}")
    if r2.get("_cache") != "hit":
        raise RuntimeError(f"expected cache hit on second call, got _cache={r2.get('_cache')}")

    c1, c2 = r1["composite"], r2["composite"]
    # Remove cache annotation before deep-compare
    r1_cmp = {k: v for k, v in r1.items() if k != "_cache"}
    r2_cmp = {k: v for k, v in r2.items() if k != "_cache"}
    ok = r1_cmp == r2_cmp
    if verbose:
        tag = "OK" if ok else "MISMATCH"
        print(f"  verify-cache {slug}: {tag}  miss={c1:.6f}  hit={c2:.6f}", file=sys.stderr)
    return ok, c1, c2


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("content")
    parser.add_argument("--profile")
    parser.add_argument("--config")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--no-daemon", action="store_true",
                        help="Force in-process model loading (skip eval_server daemon)")
    parser.add_argument("--no-cache", action="store_true",
                        help="Skip the score cache (forces re-scoring). Daemon still holds models warm.")
    parser.add_argument("--verify-cache", action="store_true",
                        help="Round-trip cache test: flush, score twice, assert bitwise equality. "
                             "Requires the daemon to be running.")
    parser.add_argument("--suggest-weakest-dim", action="store_true",
                        help="Output JSON {slug, composite, weakest_dim, weakest_score, "
                             "dims: {...}} to stdout. For the loop's hypothesize step.")
    args = parser.parse_args()

    use_daemon = not args.no_daemon
    use_cache = not args.no_cache

    # --verify-cache mode: round-trip every configured article; exit 1 if any mismatch.
    if args.verify_cache:
        if args.config:
            config_path = os.path.join(PROJECT_ROOT, args.config) if not os.path.isabs(args.config) else args.config
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            all_ok = True
            for kw in config["keywords"]:
                slug = kw["slug"]
                content_path = os.path.join(PROJECT_ROOT, kw["content_file"])
                profile_path = os.path.join(PROJECT_ROOT, "data", "profiles", f"{slug}-profile.json")
                if not os.path.exists(content_path) or not os.path.exists(profile_path):
                    continue
                ok, miss, hit = _verify_cache_one(slug, content_path, profile_path, args.verbose)
                if not ok:
                    print(f"VERIFY-CACHE MISMATCH: {slug}  miss={miss:.6f}  hit={hit:.6f}", file=sys.stderr)
                    all_ok = False
            print("VERIFY-CACHE OK" if all_ok else "VERIFY-CACHE FAILED")
            sys.exit(0 if all_ok else 1)
        elif args.profile:
            profile_path = args.profile if os.path.isabs(args.profile) else os.path.join(PROJECT_ROOT, args.profile)
            content_path = args.content if os.path.isabs(args.content) else os.path.join(PROJECT_ROOT, args.content)
            slug = _derive_slug(content_path)
            ok, miss, hit = _verify_cache_one(slug, content_path, profile_path, args.verbose)
            print(f"VERIFY-CACHE {'OK' if ok else 'FAILED'}  miss={miss:.6f}  hit={hit:.6f}")
            sys.exit(0 if ok else 1)
        else:
            parser.error("--verify-cache requires --profile or --config")

    if args.profile:
        profile_path = args.profile if os.path.isabs(args.profile) else os.path.join(PROJECT_ROOT, args.profile)
        content_path = args.content if os.path.isabs(args.content) else os.path.join(PROJECT_ROOT, args.content)
        slug = _derive_slug(content_path)
        result = _score_one(content_path, profile_path, args.verbose, use_daemon, slug=slug, use_cache=use_cache)
        if args.suggest_weakest_dim:
            print(json.dumps(_weakest_dim_report(slug, result)))
        else:
            print(f"{result['composite']:.4f}")
    elif args.config:
        config_path = os.path.join(PROJECT_ROOT, args.config) if not os.path.isabs(args.config) else args.config
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        scores = []
        reports = []
        for kw in config["keywords"]:
            slug = kw["slug"]
            content_path = os.path.join(PROJECT_ROOT, kw["content_file"])
            profile_path = os.path.join(PROJECT_ROOT, "data", "profiles", f"{slug}-profile.json")
            if not os.path.exists(content_path) or not os.path.exists(profile_path):
                continue
            result = _score_one(content_path, profile_path, args.verbose, use_daemon,
                                slug=slug, use_cache=use_cache)
            scores.append(result["composite"])
            if args.suggest_weakest_dim:
                reports.append(_weakest_dim_report(slug, result))
        if args.suggest_weakest_dim:
            print(json.dumps(reports))
        elif scores:
            print(f"{sum(scores)/len(scores):.4f}")
        else:
            print("0.0000")
            sys.exit(1)
    else:
        parser.error("Either --profile or --config is required")


if __name__ == "__main__":
    main()
