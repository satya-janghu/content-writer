#!/usr/bin/env python3
"""
Build competitive profiles for the autoresearch evaluation harness (v3.0).

Models:
  - BAAI/bge-large-en-v1.5 — 1024-dim embeddings for topical + corpus similarity
  - urchade/gliner_large-v2.1 — zero-shot NER with custom entity types
  - BERTopic — topic modeling for subtopic clusters
  - facebook/bart-large-cnn — abstractive summaries
  - KeyBERT — BERT keyword extraction
  - textstat — multi-metric readability

Per keyword the profile stores:
  - bertopic_clusters (centroid embeddings + representative terms)
  - top_terms_bm25 (top-100 TF-IDF terms as BM25 query)
  - keybert_terms per competitor
  - gliner_entities (with type labels)
  - entity_graph (co-occurrence, NetworkX-serialized)
  - bart_summaries per competitor
  - readability + structural ranges (for per-competitor benchmarking)
  - competitor_corpus (full bodies)

Usage:
  source .venv/bin/activate
  python scripts/analyze_competitors.py
"""

import json
import os
import re
import sys
from collections import Counter

import numpy as np
import textstat
import torch
from sentence_transformers import SentenceTransformer
from sklearn.feature_extraction.text import TfidfVectorizer, CountVectorizer
from bertopic import BERTopic
# cuml UMAP+HDBSCAN runs on cupy and corrupts the torch CUDA context state in this
# multi-model pipeline (BERTopic → next keyword's GLiNER crashes). Use CPU umap+hdbscan;
# BERTopic clustering runs once per keyword and is sub-second on these section counts.
from umap import UMAP as _UMAP
from hdbscan import HDBSCAN as _HDBSCAN
_UMAP_BACKEND = "cpu"
from gliner import GLiNER
from keybert import KeyBERT
from transformers import pipeline as hf_pipeline
import networkx as nx

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEYWORDS_PATH = os.path.join(PROJECT_ROOT, "data", "keywords.json")
COMPETITORS_DIR = os.path.join(PROJECT_ROOT, "data", "competitors")
PROFILES_DIR = os.path.join(PROJECT_ROOT, "data", "profiles")

# Universal entity types for GLiNER — covers most SEO content domains
GLINER_ENTITY_TYPES = [
    "tool", "platform", "software product", "company",
    "search engine", "AI assistant",
    "metric", "KPI", "technical concept",
    "brand", "physical product", "industry body",
    "person", "location"
]

print("Loading models (first run downloads ~4 GB)...", file=sys.stderr)
EMBED_MODEL = SentenceTransformer(
    "BAAI/bge-large-en-v1.5",
    model_kwargs={"torch_dtype": torch.float16},
    device="cuda",
)
print("  bge-large-en-v1.5: loaded (1024-dim, fp16, cuda)", file=sys.stderr)

NER_MODEL = GLiNER.from_pretrained("urchade/gliner_large-v2.1", map_location="cuda")
# quantize/compile are only available on newer GLiNER builds; treat both as opportunistic.
if hasattr(NER_MODEL, "quantize"):
    try:
        _q = NER_MODEL.quantize(dtype="float16")
        if _q is not None:
            NER_MODEL = _q
    except Exception as e:
        print(f"  gliner: quantize failed ({e}); fp32 weights", file=sys.stderr)
else:
    # Skip manual fp16 cast — fp16 deberta-v3 attention can produce out-of-range
    # indices via numerical instability; fp32 is the documented-safe path.
    print("  gliner: fp32 weights (no quantize API on this build)", file=sys.stderr)
if hasattr(NER_MODEL, "compile"):
    try:
        _c = NER_MODEL.compile()
        if _c is not None:
            NER_MODEL = _c
    except Exception as e:
        print(f"  gliner: compile failed ({e}); eager mode", file=sys.stderr)

def _safe_gliner_budget(types: list[str], hard_cap: int = 384, margin: int = 20) -> int:
    """384-token cap MINUS the entity-prompt prefix that GLiNER prepends.
    See https://github.com/urchade/GLiNER/issues/185 and /issues/231."""
    tok = NER_MODEL.data_processor.transformer_tokenizer
    prompt_text = " ".join(f"<<ENT>> {t} <<SEP>>" for t in types)
    prompt_len = len(tok.encode(prompt_text, add_special_tokens=True))
    return max(64, hard_cap - prompt_len - margin)

MAX_GLINER_TOKENS = _safe_gliner_budget(GLINER_ENTITY_TYPES)
print(f"  gliner_large-v2.1: loaded (cuda, fp16, compiled); chunk budget = {MAX_GLINER_TOKENS} tokens", file=sys.stderr)

KEYBERT_MODEL = KeyBERT(model=EMBED_MODEL)
KEYBERT_VECTORIZER = CountVectorizer(ngram_range=(1, 3), stop_words="english", max_features=2000)
print("  KeyBERT: loaded (uses BGE embeddings, bounded CountVectorizer)", file=sys.stderr)
print(f"  topic-modeling backend: UMAP/HDBSCAN = {_UMAP_BACKEND}", file=sys.stderr)

print("  loading BART summarizer (lazy — first use)...", file=sys.stderr)
SUMMARIZER = None  # Lazy-load only when needed


def get_summarizer():
    global SUMMARIZER
    if SUMMARIZER is None:
        SUMMARIZER = hf_pipeline(
            "summarization",
            model="facebook/bart-large-cnn",
            device=0,
            torch_dtype=torch.float16,
        )
        # bart-large-cnn ships with tokenizer.model_max_length = sentinel (1e18),
        # so truncation=True is a no-op and chunks >1024 tokens trigger a CUDA
        # position-embedding assert. Pin it to the model's real limit.
        SUMMARIZER.tokenizer.model_max_length = 1024
        print("  bart-large-cnn: loaded", file=sys.stderr)
    return SUMMARIZER


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


def read_competitor_body(filepath: str) -> str:
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
            wc = len(cleaned.split())
            if wc >= min_words:
                sections.append({"heading": current_heading, "text": cleaned, "word_count": wc})
            current_heading = m.group(2).strip()
            current_lines = []
        else:
            current_lines.append(line)
    text = "\n".join(current_lines).strip()
    cleaned = strip_markdown(text)
    wc = len(cleaned.split())
    if wc >= min_words:
        sections.append({"heading": current_heading, "text": cleaned, "word_count": wc})
    return sections


def tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z][a-z0-9]+", text.lower()) if len(t) >= 2]


def competitive_range(values: list[float]) -> dict:
    if not values:
        return {"floor": 0.0, "target": 0.0, "mid": 0.0, "values": []}
    f, t = float(min(values)), float(max(values))
    return {"floor": f, "target": t, "mid": (f + t) / 2.0, "values": [float(v) for v in values]}


# --- Readability & structural ---

def compute_readability(text: str) -> dict:
    return {
        "flesch_reading_ease": textstat.flesch_reading_ease(text),
        "flesch_kincaid_grade": textstat.flesch_kincaid_grade(text),
        "gunning_fog": textstat.gunning_fog(text),
        "dale_chall": textstat.dale_chall_readability_score(text),
    }


def compute_structural(body: str) -> dict:
    lines = body.split("\n")
    headings = len([l for l in lines if re.match(r"^#{1,6}\s", l)])
    h2 = len([l for l in lines if re.match(r"^##\s", l)])
    h3 = len([l for l in lines if re.match(r"^###\s", l)])
    paragraphs = len([p for p in body.split("\n\n") if p.strip()])
    list_items = len(re.findall(r"^[-*]\s", body, re.MULTILINE))
    tables = len(re.findall(r"^\|.*\|$", body, re.MULTILINE))
    images = len(re.findall(r"\[IMAGE:", body)) + len(re.findall(r"!\[", body))
    word_count = len(strip_markdown(body).split())

    answer_blocks = 0
    total_h2 = 0
    sections = body.split("\n##")
    for sec in sections[1:]:
        total_h2 += 1
        parts_after = sec.split("\n", 1)
        if len(parts_after) < 2:
            continue
        first_para = parts_after[1].lstrip().split("\n\n", 1)[0]
        wc = len(strip_markdown(first_para).split())
        if 40 <= wc <= 100:
            answer_blocks += 1

    return {
        "heading_count": headings,
        "h2_count": h2,
        "h3_count": h3,
        "paragraph_count": paragraphs,
        "list_item_count": list_items,
        "table_count": tables,
        "image_count": images,
        "word_count": word_count,
        "answer_blocks": answer_blocks,
        "total_h2": total_h2,
        "answer_block_ratio": answer_blocks / total_h2 if total_h2 > 0 else 0.0,
    }


def compute_keyword_density(body: str, keyword: str) -> float:
    cleaned = strip_markdown(body).lower()
    words = cleaned.split()
    if not words:
        return 0.0
    kw_lower = keyword.lower()
    count = cleaned.count(kw_lower)
    kw_words = len(kw_lower.split())
    return count / (len(words) / kw_words) if words else 0.0


# --- GLiNER entities ---

def extract_gliner_entities(text: str, max_tokens_per_chunk: int | None = None) -> list[dict]:
    """Run GLiNER over token-bounded chunks. Budget = 384 - entity-prompt prefix - margin.
    Hard-truncates any single sentence that exceeds the budget."""
    if max_tokens_per_chunk is None:
        max_tokens_per_chunk = MAX_GLINER_TOKENS
    tok = NER_MODEL.data_processor.transformer_tokenizer
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
    all_entities = []
    for chunk in chunks:
        if not chunk.strip():
            continue
        ents = NER_MODEL.predict_entities(chunk, GLINER_ENTITY_TYPES, threshold=0.4)
        all_entities.extend(ents)
    # Deduplicate by (lowercased text, label)
    seen = set()
    dedup = []
    for e in all_entities:
        key = (e["text"].strip().lower(), e["label"])
        if key not in seen:
            seen.add(key)
            dedup.append({"text": e["text"].strip(), "label": e["label"], "score": float(e["score"])})
    return dedup


# --- BERTopic clusters ---

def build_bertopic_clusters(all_sections: list[dict]) -> list[dict]:
    """Cluster competitor sections into interpretable topics via BERTopic."""
    if len(all_sections) < 4:
        return []
    texts = [s["heading"] + ". " + s["text"] for s in all_sections]

    if _UMAP_BACKEND == "cuml":
        umap = _UMAP(n_neighbors=15, n_components=5, min_dist=0.0, metric="cosine", random_state=42)
        hdbscan = _HDBSCAN(min_cluster_size=2, metric="euclidean", prediction_data=True)
    else:
        umap = _UMAP(n_neighbors=15, n_components=5, min_dist=0.0, metric="cosine", random_state=42)
        hdbscan = _HDBSCAN(min_cluster_size=2)
    topic_model = BERTopic(
        embedding_model=EMBED_MODEL,
        umap_model=umap,
        hdbscan_model=hdbscan,
        min_topic_size=2,
        verbose=False,
        calculate_probabilities=False,
    )
    try:
        topics, _ = topic_model.fit_transform(texts)
    except Exception as e:
        print(f"  BERTopic failed: {e}", file=sys.stderr)
        return []

    # topic_model.get_topic_info() returns DataFrame
    topic_info = topic_model.get_topic_info()
    total_competitors = len({s["competitor"] for s in all_sections})

    clusters = []
    for tid in topic_info["Topic"].tolist():
        if tid == -1:  # outlier topic
            continue
        member_indices = [i for i, t in enumerate(topics) if t == tid]
        if not member_indices:
            continue
        competitor_set = sorted({all_sections[i]["competitor"] for i in member_indices})
        top_terms = [term for term, _ in topic_model.get_topic(tid)[:10]]
        headings = list({all_sections[i]["heading"] for i in member_indices})[:5]
        # Centroid from embedding
        member_embs = EMBED_MODEL.encode([texts[i] for i in member_indices], show_progress_bar=False, batch_size=32)
        member_embs_norm = member_embs / np.linalg.norm(member_embs, axis=1, keepdims=True)
        centroid = member_embs_norm.mean(axis=0)
        centroid = centroid / np.linalg.norm(centroid)
        clusters.append({
            "id": int(tid),
            "centroid": centroid.tolist(),
            "top_terms": top_terms,
            "competitor_set": competitor_set,
            "competitor_count": len(competitor_set),
            "total_competitors": total_competitors,
            "weight": len(competitor_set) / total_competitors,
            "representative_headings": headings,
            "member_count": len(member_indices),
        })
    return clusters


# --- Entity graph ---

def build_entity_graph(sections: list[dict], entity_sets: list[list[dict]]) -> dict:
    """Build co-occurrence graph from entities per section.
    sections: list of {competitor, text}
    entity_sets: list of entity lists, one per competitor's merged entities
    Returns serialized adjacency list.
    """
    # Build competitor-level graph: entities co-occurring within same section are connected
    G = nx.Graph()

    # Group sections by competitor to detect co-occurrence
    sections_by_comp = {}
    for s in sections:
        sections_by_comp.setdefault(s["competitor"], []).append(s)

    # For each competitor, for each section, find entities present in that section's text,
    # add edges among them (weighted by count)
    for comp_idx, (comp, comp_sections) in enumerate(sections_by_comp.items()):
        comp_entities = entity_sets[comp_idx] if comp_idx < len(entity_sets) else []
        entity_texts = [e["text"].lower() for e in comp_entities]
        for sec in comp_sections:
            text_lower = sec["text"].lower()
            present = [et for et in entity_texts if et in text_lower]
            # Add edges among all pairs
            for i, a in enumerate(present):
                if not G.has_node(a):
                    G.add_node(a, count=1)
                else:
                    G.nodes[a]["count"] = G.nodes[a].get("count", 0) + 1
                for b in present[i + 1:]:
                    if G.has_edge(a, b):
                        G[a][b]["weight"] += 1
                    else:
                        G.add_edge(a, b, weight=1)

    # Serialize as adjacency dict (only edges with weight >= 2 to filter noise)
    edges = [(u, v, d["weight"]) for u, v, d in G.edges(data=True) if d["weight"] >= 2]
    nodes = [{"entity": n, "count": d.get("count", 0)} for n, d in G.nodes(data=True)]
    return {
        "nodes": nodes,
        "edges": [{"a": u, "b": v, "weight": w} for u, v, w in edges],
    }


# --- KeyBERT keywords ---

def extract_keybert(text: str, top_n: int = 20) -> list[dict]:
    keywords = KEYBERT_MODEL.extract_keywords(
        text,
        vectorizer=KEYBERT_VECTORIZER,
        top_n=top_n,
        use_mmr=True,
        diversity=0.5,
    )
    return [{"term": kw, "score": float(sc)} for kw, sc in keywords]


# --- BART summary ---

def summarize(text: str, max_len: int = 180, min_len: int = 80) -> str:
    """Generate abstractive summary. Handles long text via chunking."""
    summarizer = get_summarizer()
    # BART handles ~1024 tokens; chunk longer text
    words = text.split()
    if len(words) <= 800:
        try:
            out = summarizer(text, max_length=max_len, min_length=min_len, do_sample=False, truncation=True)
            return out[0]["summary_text"]
        except Exception:
            return text[:500]
    # Multi-chunk: summarize chunks (batched on GPU), then summarize summaries
    chunk_size = 800
    chunks = [" ".join(words[i:i + chunk_size]) for i in range(0, len(words), chunk_size)][:6]
    try:
        outs = summarizer(chunks, max_length=100, min_length=40, do_sample=False, truncation=True, batch_size=4)
        chunk_summaries = [o["summary_text"] for o in outs]
    except Exception:
        chunk_summaries = [c[:200] for c in chunks]
    combined = " ".join(chunk_summaries)
    if len(combined.split()) > 300:
        try:
            out = summarizer(combined, max_length=max_len, min_length=min_len, do_sample=False, truncation=True)
            return out[0]["summary_text"]
        except Exception:
            return combined[:600]
    return combined


# --- TF-IDF top terms for BM25 query ---

# NOTE: Per the v3.3 addendum, GEO dimensions use ABSOLUTE research-derived
# targets (GEO-SFE / Aggarwal KDD'24 / Lee 2026), not competitor-relative
# normalisation. SERP-ranked competitors have no validated GEO merit
# (Chen et al. 2025 found SERP↔GEO ρ ≈ 0 across 19k queries). Therefore
# no per-competitor GEO structural stats are computed here.


def generate_geo_sim_queries(
    keyword: str,
    content_type: str,
    competitor_summaries: list[str],
) -> tuple[list[str], str]:
    """Three natural-language queries a user would type into an AI search engine.

    Gemini-generated once per profile and frozen into the profile JSON so
    scoring passes are deterministic. Raises `evaluate.ScoringError` on
    Gemini failure — no template fallback (decisions #33). Profile builds
    are a manual offline step; a silent fallback would bake lower-quality
    queries into every subsequent scoring run.

    Returns (queries, "gemini").
    """
    import evaluate

    _ = content_type  # accepted for legacy signature; no longer switched on

    excerpts = "\n\n---\n\n".join(
        f"[Competitor {i+1}]\n{s[:500]}" for i, s in enumerate(competitor_summaries[:3])
    ) or "(no competitor summaries available)"

    prompt = f"""You are simulating how real users query AI search engines (ChatGPT, Perplexity, Google AI Overviews).

Target topic: "{keyword}"
Content type: {content_type}
Reference competitor summaries:
{excerpts}

Produce exactly 3 distinct natural-language queries a person would type into an AI search engine to get answers covered by this topic. The queries should:
- Sound like real user speech, not keyword-stuffed SEO phrasing
- Differ in angle (e.g. definition, comparison, application, evaluation)
- Stay on topic — don't drift to adjacent subjects

Output ONLY valid JSON with this schema, no prose:
{{"queries": ["<query1>", "<query2>", "<query3>"]}}"""

    # Profile-build is an offline, manual step — surface Gemini failure loudly
    # rather than silently burning in template queries (which would then poison
    # every subsequent scoring run). Per decisions #33, no fallbacks.
    data = evaluate._gemini_generate_json(prompt, log_tag=f"geo_queries[{content_type}]")

    queries = data.get("queries") if isinstance(data, dict) else None
    if not isinstance(queries, list) or len(queries) < 2:
        raise evaluate.ScoringError(
            "geo_sim_queries",
            f"Gemini returned bad shape ({repr(queries)[:120]}); re-run or inspect the prompt",
        )

    # Keep at most 3 non-empty strings.
    cleaned_qs = [str(q).strip() for q in queries if str(q).strip()][:3]
    if len(cleaned_qs) < 2:
        raise evaluate.ScoringError(
            "geo_sim_queries",
            f"fewer than 2 non-empty queries after cleaning ({repr(cleaned_qs)}); re-run or inspect prompt",
        )
    return (cleaned_qs, "gemini")


def extract_top_bm25_terms(corpus: list[str], top_n: int = 100) -> list[str]:
    """Top TF-IDF terms across the competitor corpus, used as BM25 'query' at scoring time."""
    vectorizer = TfidfVectorizer(
        max_features=500,
        ngram_range=(1, 2),
        stop_words="english",
        min_df=1,
        sublinear_tf=True,
    )
    tfidf = vectorizer.fit_transform(corpus)
    mean_scores = np.asarray(tfidf.mean(axis=0)).flatten()
    feature_names = vectorizer.get_feature_names_out()
    ranked = sorted(zip(feature_names, mean_scores), key=lambda kv: -kv[1])[:top_n]
    return [term for term, _ in ranked]


# --- Main profile builder ---

def build_profile(keyword: str, slug: str, silo: str, competitor_files: list[str],
                  content_type: str = "guide") -> dict:
    print(f"\nBuilding profile: {slug}", file=sys.stderr)

    competitor_bodies = {}
    for cf in competitor_files:
        name = os.path.splitext(os.path.basename(cf))[0]
        path = os.path.join(COMPETITORS_DIR, slug, cf)
        competitor_bodies[name] = read_competitor_body(path)

    per_competitor = []
    all_sections = []
    competitor_corpus = []
    competitor_entities = []
    competitor_keybert = []
    competitor_summaries = []

    for name, body in competitor_bodies.items():
        cleaned = strip_markdown(body)
        wc = len(cleaned.split())
        competitor_corpus.append(cleaned)

        print(f"  [{name}] sections...", file=sys.stderr)
        sections = extract_sections(body)
        for s in sections:
            s["competitor"] = name
            all_sections.append(s)

        print(f"  [{name}] readability + structural...", file=sys.stderr)
        readability = compute_readability(cleaned)
        density = compute_keyword_density(body, keyword)
        structural = compute_structural(body)

        def _gpu_flush():
            if torch.cuda.is_available():
                torch.cuda.synchronize()
                torch.cuda.empty_cache()

        print(f"  [{name}] GLiNER entities...", file=sys.stderr)
        entities = extract_gliner_entities(cleaned)
        competitor_entities.append(entities)
        _gpu_flush()

        print(f"  [{name}] KeyBERT keywords...", file=sys.stderr)
        keybert_terms = extract_keybert(cleaned, top_n=20)
        competitor_keybert.append(keybert_terms)
        _gpu_flush()

        print(f"  [{name}] BART summary...", file=sys.stderr)
        summary = summarize(cleaned)
        competitor_summaries.append(summary)
        _gpu_flush()

        per_competitor.append({
            "name": name,
            "word_count": wc,
            "readability": readability,
            "keyword_density": density,
            "structural": structural,
            "section_count": len(sections),
            "entity_count": len(entities),
        })

        if torch.cuda.is_available():
            torch.cuda.synchronize()
            torch.cuda.empty_cache()

    print(f"  BERTopic clustering...", file=sys.stderr)
    bertopic_clusters = build_bertopic_clusters(all_sections)
    print(f"  found {len(bertopic_clusters)} topical clusters", file=sys.stderr)

    print(f"  top BM25 query terms...", file=sys.stderr)
    top_terms_bm25 = extract_top_bm25_terms(competitor_corpus, top_n=100)

    print(f"  entity co-occurrence graph...", file=sys.stderr)
    entity_graph = build_entity_graph(all_sections, competitor_entities)

    # Ranges
    word_counts = [c["word_count"] for c in per_competitor]
    fre = [c["readability"]["flesch_reading_ease"] for c in per_competitor]
    fog = [c["readability"]["gunning_fog"] for c in per_competitor]
    dale = [c["readability"]["dale_chall"] for c in per_competitor]
    fkg = [c["readability"]["flesch_kincaid_grade"] for c in per_competitor]
    densities = [c["keyword_density"] for c in per_competitor]
    headings = [c["structural"]["heading_count"] for c in per_competitor]
    h2s = [c["structural"]["h2_count"] for c in per_competitor]
    lists = [c["structural"]["list_item_count"] for c in per_competitor]
    tables = [c["structural"]["table_count"] for c in per_competitor]
    images = [c["structural"]["image_count"] for c in per_competitor]
    answer_ratios = [c["structural"]["answer_block_ratio"] for c in per_competitor]

    # GEO simulation queries (frozen into profile so scoring stays deterministic).
    # Note: no competitor-GEO aggregates are computed — dim 12 uses absolute
    # research-derived targets, not SERP-competitor averages.
    print(f"  GEO sim queries ({content_type})...", file=sys.stderr)
    geo_sim_queries, geo_queries_source = generate_geo_sim_queries(
        keyword, content_type, competitor_summaries,
    )
    print(f"    source: {geo_queries_source}; {len(geo_sim_queries)} queries", file=sys.stderr)

    profile = {
        "slug": slug,
        "keyword": keyword,
        "silo": silo,
        "schema_version": "3.0",
        "competitor_count": len(per_competitor),
        "competitors": [{"name": c["name"], "word_count": c["word_count"]} for c in per_competitor],
        "bertopic_clusters": bertopic_clusters,
        "top_terms_bm25": top_terms_bm25,
        "competitor_entities": competitor_entities,
        "competitor_keybert": competitor_keybert,
        "competitor_summaries": competitor_summaries,
        "entity_graph": entity_graph,
        "competitor_corpus": competitor_corpus,
        "ranges": {
            "word_count": competitive_range(word_counts),
            "readability": {
                "flesch_reading_ease": competitive_range(fre),
                "gunning_fog": competitive_range(fog),
                "dale_chall": competitive_range(dale),
                "flesch_kincaid_grade": competitive_range(fkg),
            },
            "keyword_density": competitive_range(densities),
            "structural": {
                "heading_count": competitive_range(headings),
                "h2_count": competitive_range(h2s),
                "list_item_count": competitive_range(lists),
                "table_count": competitive_range(tables),
                "image_count": competitive_range(images),
                "answer_block_ratio": competitive_range(answer_ratios),
            },
        },
        "content_length_policy": {
            "hard_min": int(min(word_counts) * 0.7),
            "soft_min": int(min(word_counts)),
            "soft_max": min(7000, int(max(word_counts) * 2.0)),
            "hard_max": 7000,
        },
        "per_competitor_detail": per_competitor,
        "geo_sim_queries": geo_sim_queries,
        "geo_sim_queries_source": geo_queries_source,
    }

    return profile


def main():
    os.makedirs(PROFILES_DIR, exist_ok=True)
    with open(KEYWORDS_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

    for kw in config["keywords"]:
        slug = kw["slug"]
        keyword = kw["keyword"]
        silo = kw.get("silo", "")
        competitor_files = [os.path.basename(u["file"]) for u in kw["competitors"]]
        content_type = kw.get("content_type", "guide")
        profile = build_profile(keyword, slug, silo, competitor_files, content_type)
        out_path = os.path.join(PROFILES_DIR, f"{slug}-profile.json")
        with open(out_path, "w") as f:
            json.dump(profile, f, indent=2)
        size_kb = os.path.getsize(out_path) / 1024
        print(f"  wrote {out_path} ({size_kb:.0f} KB)", file=sys.stderr)


if __name__ == "__main__":
    main()
