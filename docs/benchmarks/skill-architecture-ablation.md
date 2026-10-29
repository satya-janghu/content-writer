# Skill architecture ablation: multi-phase vs single-pass vs compressed

Comparison of three content-generation approaches across the four test keywords, scored against the existing competitor profiles using the production v3.5 harness. No changes to scoring semantics, weights, or baselines — apples-to-apples.

- **Multi-phase**: the 4-phase skill (outline, draft, humanize, voice check), 15 files. The reference.
- **Single-pass full**: all core rules consolidated into one file, 1 file. Phase coordination removed; cross-file duplication collapsed.
- **Compressed**: 1 file, directives only. Verbose rules reduced to directives, related rules merged, guardrails (banned words, hard refusals) preserved verbatim.

All three articles were produced by the same generator (Opus 4.6) and scored with the v3.5 11-dim harness (which still included the Gemini judge). Scoring ran through the warm `eval_server.py` daemon.

---

## 1. Skill footprint

| Skill | Files | Words | Chars | % of baseline size |
|---|---:|---:|---:|---:|
| Multi-phase (current) | 15 | 17,026 | 112,786 | 100% |
| Single-pass full | 1 | 6,151 | 40,504 | **36.1%** |
| Compressed | 1 | 2,533 | 17,218 | **14.9%** |

The single-pass full dropped to 36% despite preserving every core rule because the source split the same rules across 2-3 files: banned words existed in `banned-words.md` + `humanize-vocabulary.md` + `quality-rules.md`; answer-block and structural targets in `structural-rules.md` + `seo-writer.md` + `seo-writer-checklist.md` + `aeo-rules.md`; direct-answer patterns in `aeo-rules.md` + `structural-rules.md` + `seo-writer.md`. Collapsing those duplicates alone accounts for most of the size reduction.

The compressed variant landed at **41.2% of single-pass full**, squarely in the 30-50% target band.

---

## 2. Regeneration wall-clock (4 keywords, parallel)

| Variant | Total (parallel, 4 workers) | Per-article range | Longest article |
|---|---:|---|---|
| Multi-phase (reference) | ~800-900s per article | 771-900s+ | `seo-for-roofing` ran 900s timeout in prior production loops |
| Single-pass full | **388.6s total** | 141-388s | `seo-for-roofing` |
| Compressed | **432.9s total** | 168-432s | `search-intent` |

### Per-article timings

| Keyword | Single (s) | Compressed (s) |
|---|---:|---:|
| aeo-vs-seo | 141.0 | 168.6 |
| seo-for-saas | 176.4 | 240.0 |
| search-intent | 295.1 | 432.9 |
| seo-for-roofing | 388.6 | 190.8 |

**Speed interpretation.** Both variants finish the whole 4-keyword batch in ~6-7 minutes vs ~13+ minutes per single article under the multi-phase pipeline. The compressed variant is not uniformly faster than single-pass full — the smaller ruleset seems to leave more room for the model to improvise, which takes the same or slightly more thinking tokens on some topics (search-intent is the clearest example: compressed took 432s vs single's 295s).

---

## 3. Composite scores — headline comparison

| Keyword | Baseline | Single | Compressed | Δ Single | Δ Compressed |
|---|---:|---:|---:|---:|---:|
| aeo-vs-seo | 0.8582 | 0.8353 | **0.8592** | −2.67% | **+0.12%** |
| search-intent | 0.8431 | 0.8308 | 0.8129 | −1.46% | −3.58% |
| seo-for-roofing | 0.8584 | 0.8290 | **0.8607** | −3.42% | **+0.27%** |
| seo-for-saas | 0.8556 | 0.8456 | 0.8363 | −1.17% | −2.26% |
| **AVG** | **0.8538** | **0.8352** | **0.8423** | **−2.18%** | **−1.35%** |

**Key observations:**

- **Compressed wins on average composite over single-pass full** (0.8423 vs 0.8352) — despite being 41% of its size.
- Compressed **beat or matched baseline on 2 of 4 keywords** (aeo-vs-seo +0.12%, seo-for-roofing +0.27%). No variant beat baseline on all four.
- Single-pass full never beat baseline. Its best keyword (seo-for-saas) still trailed by 1.17%.
- The worst single-variant result is compressed on search-intent (−3.58%) — driven by length and entity-coverage shortfalls (see §5.2).

---

## 4. Per-dimension averages across 4 keywords

| # | Dimension | Weight | Baseline | Single | Compressed | Δ Single | Δ Compressed |
|---|---|---:|---:|---:|---:|---:|---:|
| 1 | topical_coverage | 27.8% | 1.0000 | 1.0000 | 1.0000 | +0.00% | +0.00% |
| 2 | keyword_relevance | 11.1% | 0.8751 | 0.8843 | 0.8811 | **+1.05%** | **+0.69%** |
| 3 | corpus_match | 12.2% | 0.9189 | 0.9163 | 0.9189 | −0.28% | +0.01% |
| 4 | keyword_profile | 5.6% | 0.6344 | 0.6535 | 0.6387 | **+3.02%** | +0.68% |
| 5 | entity_coverage | 7.8% | 0.3548 | 0.3345 | 0.3628 | −5.75% | **+2.23%** |
| 6 | entity_graph | 3.3% | 0.2303 | 0.2238 | 0.2301 | −2.83% | −0.11% |
| 7 | narrative_coverage | 3.3% | 0.9590 | 0.9479 | 0.9740 | −1.16% | **+1.56%** |
| 8 | readability_structural | 8.9% | 0.7935 | 0.6984 | 0.7050 | **−11.98%** | **−11.14%** |
| 9 | content_length | 5.6% | 1.0000 | 0.9302 | 0.8918 | −6.98% | **−10.82%** |
| 10 | llm_judge | 8.9% | 0.9267 | 0.8741 | 0.9300 | **−5.67%** | +0.36% |
| 11 | banned_words | 5.6% | 1.0000 | 0.9775 | 1.0000 | −2.25% | +0.00% |

### Dimensions where both variants consistently **improved**

- **Keyword relevance** (+1.05% / +0.69%). Both single-pass approaches keep the primary keyword and its semantic variants close enough to every paragraph that the cross-encoder reranker scores it higher than the multi-phase output.
- **Keyword profile** (+3.02% / +0.68%). KeyBERT extraction finds term overlap with competitors slightly better than baseline on 3 of 4 keywords for single (best on seo-for-saas +9.55%), and on 3 of 4 for compressed.

### Dimensions where both variants consistently **regressed**

- **Readability + structural (−11.98% / −11.14%)**. This is the biggest loss for both variants and deserves its own analysis (see §5.1). The structural sub-score compares the article's heading counts and answer-block ratios against the best competitor's; both single-pass variants produced less structural density, particularly fewer explicit answer blocks at the section-opener position.
- **Content length (−6.98% / −10.82%)**. Single-pass output ran shorter than baseline on every keyword except aeo-vs-seo. Compressed ran even shorter on search-intent and seo-for-saas. The length policy is band-limited: below 70% of the shortest competitor is a hard floor, and compressed's search-intent at 2456w sat close to that floor.

### Dimensions that split the two variants

- **entity_coverage (−5.75% single / +2.23% compressed)**. Compressed's shorter rule set didn't prescribe specific entity-category lists, so the model filled with its own natural entity set, which turned out to cover more of the competitor-referenced entities. Single-pass full, by explicitly listing 5 entity categories and a cluster rule, pushed the model toward a more regimented — and less competitor-overlapping — entity selection.
- **narrative_coverage (−1.16% single / +1.56% compressed)**. Compressed produced more competitor-matching narrative summaries on 3 of 4 keywords (via BART summary cosine). Single-pass was dragged down by search-intent (−9.19%).
- **llm_judge (−5.67% single / +0.36% compressed)**. This is the largest inter-variant split. Single-pass's longer ruleset hurt the judge's factual/coherence/originality composite on two of four keywords (aeo-vs-seo −15.19%, seo-for-roofing −12.50%). Compressed essentially matched baseline.
- **banned_words (−2.25% single / +0.00% compressed)**. Single tripped banned-words on seo-for-saas (score 0.91, ≈3 hits). Compressed was clean on all four. Likely the less-detailed compressed rule list was easier for the model to hold in-context and honor.

### Dimensions that are effectively locked

- **topical_coverage**: all three variants cap at 1.00 (ceiling-locked).
- **corpus_match**: within noise for all variants.
- **entity_graph**: all variants are scoring in the 0.22-0.25 band; this is a known low-score area for the project, not a variant story.

---

## 5. Per-keyword deep dive

Scoring the article body is only half the picture. The following per-keyword analysis pulls in structural metrics (word count, heading counts, bold density, tables, lists) and ties them back to the dim scores.

### 5.1 aeo-vs-seo — compressed wins

| Metric | Baseline | Single | Compressed |
|---|---:|---:|---:|
| Composite | 0.8582 | 0.8353 | **0.8592** |
| Words | 2,777 | 2,910 | 3,819 |
| H2 / H3 | 10 / 8 | 10 / 15 | 13 / 5 |
| Bold % | 13.5% | 4.7% | 1.6% |
| Tables | 1 | 1 | 3 |
| List items | 0 | 0 | 19 |

Compressed ignored the 5-10% bold range (landed at 1.6%, well below), but compensated with **more tables and more list items** — both are structural elements the scorer rewards when they match the competitor pattern. Single-pass full sat at 4.7% bold (just below the 5% floor) and had zero list items, producing a duller structural signal.

The biggest single-pass deficit here: `readability_structural −16.84%` and `llm_judge −15.19%`. The LLM judge penalty is unexpected for a rule-richer variant — one hypothesis is the single-pass version over-explained the AEO vs SEO distinction using the full `aeo-rules.md` tables, producing output the judge rated as lower-originality.

Compressed `entity_coverage` jumped +10.78% and `entity_graph` +16.61% vs baseline — the lighter rule set let the model pull more domain-natural entity clusters (e.g. "ChatGPT", "Perplexity", "Google AI Overviews" all appearing in the same paragraph).

### 5.2 search-intent — both lose; compressed loses worst

| Metric | Baseline | Single | Compressed |
|---|---:|---:|---:|
| Composite | 0.8431 | 0.8308 | **0.8129** |
| Words | 3,889 | 2,830 | 2,456 |
| H2 / H3 | 8 / 18 | 8 / 13 | 7 / 10 |
| Bold % | 11.3% | 3.9% | 6.0% |
| Tables | 3 | 1 | 1 |
| List items | 0 | 0 | 3 |

This is the only keyword where single-pass full beats compressed. Compressed shipped **37% shorter than baseline** (2456w vs 3889w) — the band-scaled content_length penalty and the entity_coverage penalty (−11.09%) both compound from that under-length.

`readability_structural` took a catastrophic −30.72% on compressed here — fewer H3s (10 vs 18), fewer tables (1 vs 3), fewer section-opener answer blocks. Single-pass also dropped on structural (−22.24%) but less severely.

Why did this keyword hit both variants harder? Likely because search-intent is *conceptually broad* — the baseline multi-phase pipeline's outline phase gave it a rich sub-section tree (18 H3s) that the single-pass variants collapsed into larger H2 sections.

### 5.3 seo-for-roofing — compressed wins

| Metric | Baseline | Single | Compressed |
|---|---:|---:|---:|
| Composite | 0.8584 | 0.8290 | **0.8607** |
| Words | 3,889 | 3,640 | 3,722 |
| H2 / H3 | 10 / 17 | 10 / 16 | 12 / 11 |
| Bold % | 9.2% | 8.1% | 7.8% |
| Tables | 2 | 1 | 2 |
| List items | 6 | 0 | 15 |

Compressed's modest win comes from `entity_coverage +14.44%` and `entity_graph +5.19%`. Roofing SEO is entity-heavy (Google Business Profile, NAP consistency, service area pages, local citations, schema types), and the compressed output clustered these more naturally.

Single-pass's −3.42% drop is driven by `readability_structural −17.85%` and `llm_judge −12.50%`. Single-pass produced a roofing article with zero list items — roofing SEO is a domain where readers expect numbered steps and checklists, and the scorer's band-score penalized the flat prose. Compressed shipped 15 list items.

### 5.4 seo-for-saas — compressed loses modestly

| Metric | Baseline | Single | Compressed |
|---|---:|---:|---:|
| Composite | 0.8556 | **0.8456** | 0.8363 |
| Words | 4,396 | 3,320 | 3,037 |
| H2 / H3 | 10 / 20 | 10 / 22 | 11 / 5 |
| Bold % | 8.7% | 9.2% | 8.3% |
| Tables | 2 | 0 | 3 |
| List items | 0 | 0 | 0 |

The only keyword where single-pass full beats compressed on composite. Compressed's **content_length −41.73%** dominates — at 3037w vs ~4400w baseline, it sits near the band-score floor. Single-pass also ran short (3320w) but not as much.

Single-pass full had zero tables (baseline and compressed both had 2-3). This cost it `readability_structural` surprisingly little (+8.81% actually, since the 22 H3s compensated on heading-depth score), but it's the clearest case of a structural rule being ignored.

Single-pass was the only variant to incur a banned_words penalty here (0.91, roughly 3 hits). Compressed stayed clean at 1.00.

---

## 6. Weakest dimension stability

The weakest dimension on every keyword for every variant is `entity_coverage`. This is unchanged from the multi-phase baseline — entity coverage has been this project's ceiling-bound weakness for many iterations of the autoresearch loop. Neither single-pass nor compressed moves the needle on this consistently.

Scores for the weakest dim across the 12 runs:

| Keyword | Baseline | Single | Compressed |
|---|---:|---:|---:|
| aeo-vs-seo | 0.3811 | 0.3866 | **0.4222** |
| search-intent | **0.3489** | 0.3068 | 0.3102 |
| seo-for-roofing | 0.3144 | 0.2922 | **0.3598** |
| seo-for-saas | **0.3750** | 0.3522 | 0.3588 |

Compressed improves entity_coverage on 2 of 4; single-pass improves on 1 of 4.

---

## 7. Structural signature comparison

Summed across the 4 keywords:

| Metric | Baseline | Single | Compressed |
|---|---:|---:|---:|
| Total words (4 articles) | 14,951 | 12,700 | 13,034 |
| Total H2 | 38 | 38 | 43 |
| Total H3 | 63 | 66 | 31 |
| Mean bold % | 10.7% | 6.5% | 5.9% |
| Total tables | 8 | 3 | 9 |
| Total list items | 6 | 0 | 52 |

**Structural pattern differences:**

- **Bold density**: baseline averaged 10.7% (above the 5-10% target); single averaged 6.5%; compressed averaged 5.9%. Both new variants actually hew *closer* to the stated rule than the baseline does — yet they lose `readability_structural` because the scorer's band-score compares to the best competitor's bold density, not to the stated rule.
- **H3 depth**: single matched baseline (66 vs 63), compressed halved it (31). Compressed prefers fewer, longer H2 sections.
- **Tables and lists**: compressed was the most structurally active — 9 tables and 52 list items across 4 articles. Single-pass full actively *suppressed* lists (0 across all 4 articles) and used fewer tables. This is likely why single-pass lost `readability_structural` — the scorer rewards structured elements per section, and zero list items is a penalty.
- **Word count**: single shipped 15.1% fewer words than baseline total; compressed 12.8% fewer. Both run short; compressed's length is less uniform (longer aeo-vs-seo, shorter search-intent and seo-for-saas).

---

## 8. Interpretation

### Why compressed sometimes beats single-pass full

The single-pass full skill inherited every directive from the source files — including ones that *conflict* at generation time. For example, `humanize-formatting.md` says bold should be 5-10% of body word count; `structural-rules.md` says each H2 should have at least one structured element; `seo-writer-checklist.md` lists a flat "bold sparingly" rule. When all three are in-context, the model hedges (lower bold %, fewer lists, less emphasis). Compressed distilled those into a single consistent directive ("5-10% bold, ≥1 structured element per H2"), and the model executed it more boldly.

### Why both variants lose readability_structural

The `readability_structural` scorer compares structural density to the **best competitor** on each sub-metric. Baseline was tuned over many autoresearch loop iterations to match that ceiling; the single-pass variants have not been tuned. The fact that both new variants lose the same amount (~11%) on this dimension suggests the loss is not about single-pass vs compressed — it's about *not having been through the tuning loop*. A couple of rule-file iterations focused on answer-block cadence and structural-element distribution would likely close most of this gap.

### Why the LLM judge splits the two variants so hard

On `llm_judge`, compressed matches baseline (+0.36%) and single-pass drops (−5.67%). The judge scores factual accuracy + coherence + originality. The most likely driver: the single-pass full skill contained the full template scaffolds from `seo-writer-templates.md` (6 content-type templates with explicit section names). The model hewed too close to those templates on some topics (aeo-vs-seo, seo-for-roofing) and the judge marked it down on originality. Compressed has only minimal skeleton hints; the model drafted more naturally.

This is counter-intuitive — you'd expect *more* rules to produce *better* output. On the LLM judge specifically, the opposite happens because the judge penalizes template-like structure.

### Why compressed still loses on average

Compressed loses on search-intent and seo-for-saas primarily due to **content length**. The compressed skill kept the length bands but dropped the "under-length is worse than over-length" emphasis and the explicit word-count targets per topic type. The model interpreted "2,000-2,800 for explainers" as "hit the floor" and shipped 2456w on search-intent, 3037w on seo-for-saas. The single-pass full version had the same range table but also carried the "articles over 3,000 words should aim for 50+ entities" language, which implicitly pulled length up.

---

## 9. Summary table

| Axis | Baseline | Single-pass full | Compressed |
|---|---|---|---|
| Skill size | 17,026 words / 15 files | 6,151 words / 1 file | 2,533 words / 1 file |
| Wall-clock (4 parallel) | ~800s+/article | 388.6s total | 432.9s total |
| Avg composite | 0.8538 | 0.8352 (−2.18%) | 0.8423 (−1.35%) |
| Wins vs baseline | — | 0 of 4 | 2 of 4 |
| Biggest dim regression | — | readability_structural −11.98% | readability_structural −11.14% |
| Biggest dim gain | — | keyword_profile +3.02% | entity_coverage +2.23% |
| LLM judge vs baseline | — | −5.67% | +0.36% |
| Banned-words hits | 0 | 3 (on seo-for-saas) | 0 |
| Notable per-keyword | — | best on seo-for-saas (−1.17%) | 2 wins (aeo-vs-seo +0.12%, seo-for-roofing +0.27%) |


---

## Post-ablation fixes

Two targeted fixes were applied to the promoted compressed skill, then the four articles were regenerated through the production `regen.py` entry point and re-scored.

### Composite scores

Avg composite −0.26% vs multi-phase baseline; +1.11% vs pre-fix compressed.

| Keyword | Post-fix | Multi-phase baseline | Pre-fix compressed | Δ baseline | Δ pre-fix |
|---|---:|---:|---:|---:|---:|
| aeo-vs-seo | **0.8715** | 0.8582 | 0.8592 | **+1.55%** | +1.43% |
| search-intent | 0.8287 | 0.8431 | 0.8129 | −1.71% | +1.94% |
| seo-for-roofing | **0.8597** | 0.8584 | 0.8607 | **+0.15%** | −0.12% |
| seo-for-saas | 0.8465 | 0.8556 | 0.8363 | −1.06% | +1.22% |
| **AVG** | **0.8516** | 0.8538 | 0.8423 | **−0.26%** | **+1.11%** |

Two keywords (aeo-vs-seo, seo-for-roofing) now beat the original multi-phase baseline. The other two are within 2% of baseline. This is a substantial improvement over the pre-fix compressed in aggregate.

### Fix A (content length): worked uniformly

| Keyword | Pre-fix words | Post-fix words | Pre-fix dim score | Post-fix dim score |
|---|---:|---:|---:|---:|
| aeo-vs-seo | 3912 | 3320 | 0.9843 | 1.0000 |
| search-intent | 2456 | 3448 | 1.0000 | 1.0000 |
| seo-for-roofing | 3805 | 3957 | 1.0000 | 1.0000 |
| seo-for-saas | 3037 | 4148 | 0.5827 | 1.0000 |

Biggest absolute gain: **seo-for-saas +71.6%** on the `content_length` dim. The two keywords that were under-length pre-fix (search-intent at 2456w, seo-for-saas at 3037w) both landed comfortably inside the range post-fix (3448w, 4148w). aeo-vs-seo dropped from 3912w to 3320w — still healthy, probably because the model now better internalized "aim for the upper half of the comparison range (2800-3500w)" instead of drifting past it.

Fix A is doing what it was supposed to. No further work needed there.

### Fix B (structural density): mixed

| Keyword | Pre-fix `readability_structural` | Post-fix | Δ pre-fix | Δ vs multi-phase baseline |
|---|---:|---:|---:|---:|
| aeo-vs-seo | 0.7069 | 0.8772 | **+24.09%** | +3.97% |
| seo-for-roofing | 0.8381 | 0.8571 | +2.27% | −0.46% |
| search-intent | 0.4773 | 0.4613 | −3.35% | **−33.04%** |
| seo-for-saas | 0.7979 | 0.6558 | **−17.81%** | **−15.94%** |

Fix B helped aeo-vs-seo enormously (+24%) and seo-for-roofing modestly (+2%), but:

- **search-intent** — fix barely moved the needle (−3.35%); this dim was already bad pre-fix (−31% vs baseline) and remains bad (−33%). The answer-block sub-metric is the culprit: `answer_ratio=0.333` vs `max_comp_answer_ratio=0.7` — only 1/3 of H2 sections lead with an answer block even after the explicit rule.
- **seo-for-saas** — fix *worsened* this keyword (−17.81% vs pre-fix). The 4148w saas article now distributes the structural budget differently, and the scorer's answer-block sub-ratio is worse than pre-fix. Pre-fix saas was basically fine on this dim at −2.67% from baseline; post-fix it's at −15.94%.

The fix asymmetry suggests the rule's wording ("every major H2 section must contain at least one structured element, every H2 section opens with a direct-answer block") is being interpreted as a structural ceiling for short articles (good — aeo-vs-seo) and as a reshuffling directive for long articles (bad — seo-for-saas).

### Word counts, structural metrics

| Keyword | Post-fix words | Post-fix H2 / H3 | Baseline words | Baseline H2 / H3 |
|---|---:|---|---:|---|
| aeo-vs-seo | 3,320 | 10 / 17 | 2,777 | 10 / 8 |
| search-intent | 3,448 | 8 / 17 | 3,889 | 8 / 18 |
| seo-for-roofing | 3,957 | 10 / 15 | 3,889 | 10 / 17 |
| seo-for-saas | 4,148 | 11 / 18 | 4,396 | 10 / 20 |
| Total | 14,873 | 39 / 67 | 14,951 | 38 / 63 |

Post-fix total is almost identical to baseline total word count (14,873 vs 14,951). H3 count is slightly higher post-fix (67 vs 63) — the "break H2s into H3s" guidance from Fix B is working. So the structural issue is not quantity of headings, it's the answer-block cadence specifically.

### Regen wall-clock (sequential, production `regen.py`)

| Keyword | Time |
|---|---:|
| aeo-vs-seo | 360.2s |
| search-intent | 585.5s |
| seo-for-roofing | 355.0s |
| seo-for-saas | 378.2s |
| **Total sequential** | **1,678.9s (~28 min)** |

Parallel (4 at once) would finish in ~10 minutes. Pre-fix compressed was 432.9s parallel / ~13 min. Regen is now slightly slower per article because articles are longer (Fix A is pulling them upward).

---

**Outcome:** the compressed single-pass skill (2,533 words, 14.9% of the original 17,026-word 15-file ruleset) became production (decision #41).
