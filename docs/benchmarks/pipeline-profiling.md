# Instrumented iteration profile (2026-04-16)

One fully instrumented 4-keyword iteration under the v3.6 batch architecture; these numbers drove the v3.7 redesign (decision #42).

---

## 1. Scoring per-dimension timing (from eval_server daemon log)

Captured from the first scoring call (search-intent, cold models). Subsequent calls to the same dims are much faster because GPU tensors are warm.

| Dimension | Cold (1st call) | Warm (subsequent) | Notes |
|---|---:|---:|---|
| topical_coverage | 0.32s | 0.35-0.41s | BERTopic + BGE. Consistent. |
| keyword_relevance | 0.28s | 0.27-0.30s | BGE reranker. Consistent. |
| corpus_match | 0.10s | 0.12-0.13s | BM25 + TF-IDF + BGE. Fast. |
| keyword_profile | 0.93s | 0.96-0.98s | KeyBERT. Consistent. |
| **entity_coverage** | **53.5s** | **1.1-1.2s** | GLiNER NER. **Cold start penalty: ~52s.** |
| entity_graph | 0.94s | 1.1s | NetworkX. Consistent. |
| narrative_coverage | 2.98s | 2.9-3.9s | BART summarizer. Moderate. |
| readability_structural | 1.03s | 0.03s | Textstat. Negligible after 1st call. |
| content_length | 0.001s | 0.001-0.002s | Pure arithmetic. |
| llm_judge | 5.71s | 6.3-6.4s | **Gemini API call.** Consistent 5-7s. |
| banned_words | 0.01s | 0.01s | Regex. Negligible. |

**Key finding:** entity_coverage's first call takes **53.5 seconds** due to GLiNER's JIT compilation or internal tensor warmup. This is why the first pre-score takes 65.8s while subsequent scores take 12-15s. Once warmed, entity_coverage drops to ~1s.

**Total scoring time breakdown:**
- Cold first call: 65.8s (entity_coverage dominates at 81%)
- Warm calls: 12-15s each (llm_judge at 5-7s dominates at ~45%)

The Gemini judge call is the most expensive per-scoring-call at 5-7s, but it's consistent and non-blocking. No timeouts or retries were observed during this batch.

---

## 2. Daemon warmup timing

| Model | Load time | Cumulative | Notes |
|---|---:|---:|---|
| bge-large-en-v1.5 | 5.0s | 5.0s | Sentence embeddings |
| bge-reranker-v2-m3 | 3.2s | 8.2s | Cross-encoder |
| gliner_large-v2.1 | 9.4s | 17.6s | Zero-shot NER (largest GPU model) |
| keybert | 0.0s | 17.6s | Reuses bge embedding |
| bart-large-cnn | 2.1s | 19.7s | Summarizer |
| gemini-3-flash-preview | 0.7s | 20.4s | API client only (no local model) |
| **Total warmup** | **20.4s** | | |

Free VRAM after warmup: **2.08 GB / 7.60 GB** (RTX 2070 SUPER). Models consume ~5.5 GB.

---

## 3. Summary timing table

### Per-batch budget (observed, single batch)

| Component | Time | % of batch |
|---|---:|---:|
| Daemon warmup | 20s | 1% |
| Worktree creation (4 worktrees) | 0.1s | <1% |
| **Worker phase (4 parallel)** | **1049s** | **44%** |
| &nbsp;&nbsp;Pre-score (serial daemon, cold start) | 66-106s/worker | |
| &nbsp;&nbsp;Propose (model call) | 160-474s/worker | |
| &nbsp;&nbsp;Regen (model call) | 191-671s/worker | |
| &nbsp;&nbsp;Post-score | 12-15s/worker | |
| **Merge queue (3 keepers, serial)** | **1290s** | **55%** |
| &nbsp;&nbsp;Keeper 1: regen-all + score-all + gate | 410s | PASS |
| &nbsp;&nbsp;Keeper 2: regen-all + score-all + gate | 272s | FAIL |
| &nbsp;&nbsp;Keeper 3: regen-all + score-all + gate | 609s | FAIL |
| Worktree cleanup | ~5s | <1% |
| **TOTAL** | **2364s (39.4 min)** | 100% |

### Per-worker stage budget (observed across 4 workers)

| Stage | Min | Median | Max | P95 |
|---|---:|---:|---:|---:|
| Pre-score | 66s | 87s | 106s | 106s |
| Propose | 160s | 213s | 474s | 474s |
| Regen | 191s | 238s | 671s | 671s |
| Post-score | 12s | 14s | 15s | 15s |
| **Worker total** | **450s** | **660s** | **1047s** | **1047s** |

### Per-regen-all budget (merge queue, observed across 3 cycles)

| Component | Min | Median | Max |
|---|---:|---:|---:|
| Regen-all (4 parallel, wall-clock) | 219s | 357s | 552s |
| Score-all (4 serial) | 53s | 53s | 57s |
| Gate evaluation | <1s | <1s | <1s |
| **Total per keeper** | **272s** | **410s** | **609s** |

---

## 4. Outcome of the instrumented iteration

Despite being a diagnostic run, the batch produced a real improvement:

| Keyword | Pre-batch baseline | Post-keeper-1 gate score | Δ |
|---|---:|---:|---:|
| search-intent | 0.8287 | 0.8474 | **+2.3%** |
| seo-for-saas | 0.8465 | 0.8715 | **+3.0%** |
| aeo-vs-seo | 0.8715 | 0.8742 | +0.3% |
| seo-for-roofing | 0.8597 | 0.8732 | +1.6% |
| **AVG** | **0.8516** | **0.8666** | **+1.8%** |

Keeper 1's seo-for-saas proposal improved all 4 keywords simultaneously. The committed SKILL.md edit was 3 insertions / 3 deletions targeting entity-coverage rules. The composites across all 4 keywords rose by 0.3-3.0%.

New baselines (from keeper 1's gate scores): aeo-vs-seo 0.8742, search-intent 0.8474, seo-for-roofing 0.8732, seo-for-saas 0.8715.

---

## What the profile changed

- The cross-keyword merge-queue gate took 1290 s (55% of the batch) and was removed (decision #42).
- Workers idled 255-597 s waiting for the slowest one, so the loop became async per-keyword coordinators.
- The Gemini judge cost 5-7 s per call (about 45% of warm scoring) and was removed.
- GLiNER cold start was 53.5 s versus 1.1 s warm, which justified the warm scoring daemon.
