# Sonnet 4.6 vs Opus 4.6 as the generator (2026-04-16)

**Run date / time window (UTC):** 2026-04-16 04:56:24 → 06:06:50 (~70 min wall-clock)
**Cache epoch:** `v3.7-no-judge` (10-dim composite, `n/82` weights)
**Skill under test:** `rules/content-generation/SKILL.md` (single-pass consolidated skill, decision #41)
**Bench flags:** `--timeout 1200 --tolerance 0.10`
**Raw data:** `sonnet-vs-opus.json`
---

## 1. Executive summary

| | **Opus 4.6** | **Sonnet 4.6** | Delta |
|---|---:|---:|---:|
| Average regen time | **282.2s** | **754.1s** | **+2.67× slower** |
| p95 regen time (4 samples) | 634.3s | 1063.6s | +1.68× |
| Max regen time | 634.3s | 1063.6s | +429.3s |
| Min regen time | 142.2s | 473.6s | +331.4s |
| Average composite | 0.8394 | 0.8514 | +0.0120 |
| Keywords where composite improved | — | 3 / 4 | — |
| Keywords where composite regressed | — | 1 / 4 (search-intent) | — |
| Per-dim regressions exceeding 10% tolerance | — | **4** | fail |

**Conclusion: keep Opus.** Sonnet averaged 2.67× slower (754.1 s vs 282.2 s) and crossed the 10% per-dim tolerance 4 times, 3 of them on search-intent.

The v3.7 speedups (judge removal, cross-keyword gate removal, async workers) already delivered the targeted wall-clock gains.

---

## 2. Event timeline

All 8 regens succeeded (rc=0, output file valid, non-zero body). No timeouts, no hangs. The supervisor drove Opus first then Sonnet for each keyword, in keyword order.

| # | UTC | Event | Model | Keyword | Elapsed |
|---|---|---|---|---|---:|
| 1 | 04:56:44 | REGEN_START | opus | seo-for-roofing | — |
| 2 | 05:00:03 | REGEN_END | opus | seo-for-roofing | **198.8s** |
| 3 | 05:00:11 | REGEN_START | sonnet | seo-for-roofing | — |
| 4 | 05:17:55 | REGEN_END | sonnet | seo-for-roofing | **1063.6s** |
| 5 | 05:18:03 | REGEN_START | opus | seo-for-saas | — |
| 6 | 05:28:38 | REGEN_END | opus | seo-for-saas | **634.3s** |
| 7 | 05:28:46 | REGEN_START | sonnet | seo-for-saas | — |
| 8 | 05:36:39 | REGEN_END | sonnet | seo-for-saas | **473.6s** |
| 9 | 05:36:48 | REGEN_START | opus | aeo-vs-seo | — |
| 10 | 05:39:21 | REGEN_END | opus | aeo-vs-seo | **153.4s** |
| 11 | 05:39:28 | REGEN_START | sonnet | aeo-vs-seo | — |
| 12 | 05:54:58 | REGEN_END | sonnet | aeo-vs-seo | **929.8s** |
| 13 | 05:55:05 | REGEN_START | opus | search-intent | — |
| 14 | 05:57:27 | REGEN_END | opus | search-intent | **142.2s** |
| 15 | 05:57:34 | REGEN_START | sonnet | search-intent | — |
| 16 | 06:06:44 | REGEN_END | sonnet | search-intent | **549.5s** |

Total wall-clock consumed by the bench (regens only, excluding ~10s of scoring between regens): **4145s ≈ 69 min**.

---

## 3. Headline per-keyword table

Baselines are the current `v3.7-no-judge` figures established against the already-committed `content/*.md` files. **Both bench runs regenerate fresh content from scratch** under the same `SKILL.md`, so the bench composite is a cold-start number and will generally differ from the baseline (the baseline content has been loop-optimized over prior iterations).

| keyword | baseline | opus_comp | opus_regen_s | sonnet_comp | sonnet_regen_s | Δcomposite (sonnet − opus) | speed ratio (sonnet/opus) |
|---|---:|---:|---:|---:|---:|---:|---:|
| seo-for-roofing | 0.8499 | 0.8258 | 198.8 | 0.8469 | 1063.6 | **+0.0211** | **5.35×** |
| seo-for-saas | 0.8373 | 0.8543 | 634.3 | 0.8706 | 473.6 | **+0.0163** | **0.75×** (Sonnet faster) |
| aeo-vs-seo | 0.8648 | 0.8452 | 153.4 | 0.8622 | 929.8 | **+0.0171** | **6.06×** |
| search-intent | 0.8178 | 0.8323 | 142.2 | 0.8257 | 549.5 | **−0.0066** | **3.86×** |
| — | — | — | — | — | — | — | — |
| **average** | **0.8425** | **0.8394** | **282.2** | **0.8514** | **754.1** | **+0.0120** | **2.67×** |
| median | 0.8436 | 0.8387 | 176.1 | 0.8546 | 739.6 | — | — |
| max | 0.8648 | 0.8543 | 634.3 | 0.8706 | 1063.6 | — | — |
| min | 0.8178 | 0.8258 | 142.2 | 0.8257 | 473.6 | — | — |

Observations:
- **Speed is not monotonic.** Sonnet is 0.75× (faster) on `seo-for-saas` but 5.35×-6.06× on two others. No single speed multiplier captures the relationship.
- **Quality is mostly up, but NOT on `search-intent`.** The −0.0066 composite on search-intent is small in absolute terms but Sonnet is the first variant tested that regressed against Opus on this keyword; every other engine variant in prior benches was within ±0.003.
- **Both bench composites are below baseline on 2/4 keywords** (seo-for-roofing, aeo-vs-seo). This is the "cold-start" effect — baselines reflect content that's already been through loop iterations and has accumulated small wins the agent can't rediscover from a blank sheet in one pass. It is not a bench failure; both models are evaluated against the same cold-start conditions.

---

## 4. Per-keyword, per-dimension breakdown

Columns:
- **weight**: composite weight (`n / 82` fraction under `v3.7-no-judge`).
- **opus**, **sonnet**: raw dim score in [0, 1].
- **Δraw**: `sonnet − opus`.
- **Δrel_%**: `(opus − sonnet) / opus × 100`. Positive = Sonnet regressed on this dim.
- **Δcontrib**: `Δraw × weight`. Composite contribution shift from Sonnet's regen vs Opus's.
- **flag**: ⬆ Sonnet higher, ⬇ Sonnet lower, = equal, 🚫 Sonnet regressed > 10% tolerance.

The total-contribution-shift row at the bottom of each table equals `sonnet_composite − opus_composite` within rounding.

### 4.1 seo-for-roofing

- Opus regen: **198.8s** → composite **0.8258**
- Sonnet regen: **1063.6s** (5.35× slower) → composite **0.8469**
- Composite Δ: **+0.0211** (Sonnet higher)

| dim | weight | opus | sonnet | Δraw | Δrel_% | Δcontrib | flag |
|---|---:|---:|---:|---:|---:|---:|:-:|
| topical_coverage | 0.3049 | 1.0000 | 1.0000 | +0.0000 | +0.0 | +0.00000 | = |
| keyword_relevance | 0.1220 | 0.8743 | 0.8809 | +0.0066 | −0.7 | +0.00080 | ⬆ |
| corpus_match | 0.1341 | 0.7755 | 0.8774 | +0.1019 | −13.1 | +0.01367 | ⬆ |
| keyword_profile | 0.0610 | 0.6474 | 0.6706 | +0.0232 | −3.6 | +0.00142 | ⬆ |
| **entity_coverage** | 0.0854 | **0.3448** | **0.2910** | **−0.0537** | **+15.6** | **−0.00459** | **🚫** |
| entity_graph | 0.0366 | 0.2006 | 0.1931 | −0.0075 | +3.7 | −0.00027 | ⬇ |
| narrative_coverage | 0.0366 | 0.9963 | 1.0000 | +0.0037 | −0.4 | +0.00014 | ⬆ |
| readability_structural | 0.0976 | 0.7936 | 0.8770 | +0.0834 | −10.5 | +0.00813 | ⬆ |
| content_length | 0.0610 | 1.0000 | 1.0000 | +0.0000 | +0.0 | +0.00000 | = |
| banned_words | 0.0610 | 0.9700 | 1.0000 | +0.0300 | −3.1 | +0.00183 | ⬆ |
| **total contribution shift** | — | — | — | — | — | **+0.02112** | — |

Biggest Sonnet wins: `corpus_match` (+0.1019 raw), `readability_structural` (+0.0834). Biggest loss: `entity_coverage` drops 15.6% below Opus — Sonnet is identifying fewer canonical entities.

### 4.2 seo-for-saas

- Opus regen: **634.3s** → composite **0.8543**
- Sonnet regen: **473.6s** (0.75×, faster) → composite **0.8706**
- Composite Δ: **+0.0163** (Sonnet higher)

| dim | weight | opus | sonnet | Δraw | Δrel_% | Δcontrib | flag |
|---|---:|---:|---:|---:|---:|---:|:-:|
| topical_coverage | 0.3049 | 1.0000 | 1.0000 | +0.0000 | +0.0 | +0.00000 | = |
| keyword_relevance | 0.1220 | 0.8734 | 0.8772 | +0.0038 | −0.4 | +0.00047 | ⬆ |
| corpus_match | 0.1341 | 0.9472 | 0.9789 | +0.0317 | −3.3 | +0.00425 | ⬆ |
| keyword_profile | 0.0610 | 0.6263 | 0.6053 | −0.0211 | +3.4 | −0.00128 | ⬇ |
| entity_coverage | 0.0854 | 0.3349 | 0.4132 | +0.0783 | −23.4 | +0.00669 | ⬆ |
| entity_graph | 0.0366 | 0.2251 | 0.2440 | +0.0189 | −8.4 | +0.00069 | ⬆ |
| narrative_coverage | 0.0366 | 0.8865 | 0.9221 | +0.0355 | −4.0 | +0.00130 | ⬆ |
| readability_structural | 0.0976 | 0.9239 | 0.9293 | +0.0054 | −0.6 | +0.00053 | ⬆ |
| content_length | 0.0610 | 1.0000 | 1.0000 | +0.0000 | +0.0 | +0.00000 | = |
| banned_words | 0.0610 | 0.9400 | 1.0000 | +0.0600 | −6.4 | +0.00366 | ⬆ |
| **total contribution shift** | — | — | — | — | — | **+0.01630** | — |

Sonnet wins almost every dim on this keyword — the one weakness (`keyword_profile`, −0.0211 raw) is well inside the 10% tolerance. Biggest absolute win is `entity_coverage` — a 23% improvement, the opposite of what Sonnet did on seo-for-roofing and search-intent.

### 4.3 aeo-vs-seo

- Opus regen: **153.4s** → composite **0.8452**
- Sonnet regen: **929.8s** (6.06× slower) → composite **0.8622**
- Composite Δ: **+0.0171** (Sonnet higher)

| dim | weight | opus | sonnet | Δraw | Δrel_% | Δcontrib | flag |
|---|---:|---:|---:|---:|---:|---:|:-:|
| topical_coverage | 0.3049 | 1.0000 | 1.0000 | +0.0000 | +0.0 | +0.00000 | = |
| keyword_relevance | 0.1220 | 0.8640 | 0.8862 | +0.0223 | −2.6 | +0.00272 | ⬆ |
| corpus_match | 0.1341 | 0.8296 | 0.8926 | +0.0630 | −7.6 | +0.00845 | ⬆ |
| keyword_profile | 0.0610 | 0.6417 | 0.6365 | −0.0052 | +0.8 | −0.00032 | ⬇ |
| entity_coverage | 0.0854 | 0.3904 | 0.4079 | +0.0174 | −4.5 | +0.00149 | ⬆ |
| entity_graph | 0.0366 | 0.2833 | 0.2655 | −0.0178 | +6.3 | −0.00065 | ⬇ |
| narrative_coverage | 0.0366 | 1.0000 | 1.0000 | +0.0000 | +0.0 | +0.00000 | = |
| readability_structural | 0.0976 | 0.8636 | 0.9358 | +0.0722 | −8.4 | +0.00704 | ⬆ |
| content_length | 0.0610 | 0.9973 | 1.0000 | +0.0027 | −0.3 | +0.00016 | ⬆ |
| banned_words | 0.0610 | 0.9700 | 0.9400 | −0.0300 | +3.1 | −0.00183 | ⬇ |
| **total contribution shift** | — | — | — | — | — | **+0.01706** | — |

Clean sweep of important dims for Sonnet; small losses (keyword_profile, entity_graph, banned_words) are inside tolerance and small in absolute terms. Biggest Sonnet win: `readability_structural` (+0.0722 raw), echoed across every keyword except search-intent.

### 4.4 search-intent — the only net regression

- Opus regen: **142.2s** → composite **0.8323**
- Sonnet regen: **549.5s** (3.86× slower) → composite **0.8257**
- Composite Δ: **−0.0066** (Sonnet lower)

| dim | weight | opus | sonnet | Δraw | Δrel_% | Δcontrib | flag |
|---|---:|---:|---:|---:|---:|---:|:-:|
| topical_coverage | 0.3049 | 1.0000 | 1.0000 | +0.0000 | +0.0 | +0.00000 | = |
| keyword_relevance | 0.1220 | 0.8679 | 0.8686 | +0.0007 | −0.1 | +0.00009 | ⬆ |
| corpus_match | 0.1341 | 0.8415 | 0.8736 | +0.0321 | −3.8 | +0.00431 | ⬆ |
| keyword_profile | 0.0610 | 0.6385 | 0.6619 | +0.0234 | −3.7 | +0.00143 | ⬆ |
| **entity_coverage** | 0.0854 | **0.3670** | **0.3080** | **−0.0591** | **+16.1** | **−0.00504** | **🚫** |
| **entity_graph** | 0.0366 | **0.1939** | **0.1603** | **−0.0336** | **+17.3** | **−0.00123** | **🚫** |
| narrative_coverage | 0.0366 | 0.9348 | 0.9315 | −0.0034 | +0.4 | −0.00012 | ⬇ |
| **readability_structural** | 0.0976 | **0.8078** | **0.7086** | **−0.0992** | **+12.3** | **−0.00968** | **🚫** |
| content_length | 0.0610 | 1.0000 | 1.0000 | +0.0000 | +0.0 | +0.00000 | = |
| banned_words | 0.0610 | 0.9400 | 1.0000 | +0.0600 | −6.4 | +0.00366 | ⬆ |
| **total contribution shift** | — | — | — | — | — | **−0.00658** | — |

**Three of the four bench-wide 🚫 offenders live on this keyword.** Sonnet on `search-intent` drops:
- `entity_coverage` by 16.1% (fewer canonical entities recognised)
- `entity_graph` by 17.3% (entities less co-located in paragraphs)
- `readability_structural` by 12.3% (heading density / answer-block ratio worse)

Offsetting gains on corpus_match, keyword_profile, and banned_words are too small (each <0.5% contrib) to recover the drop. Net composite regression.

The pattern is coherent — on a definitional query ("what is search intent"), Sonnet is producing a more prose-heavy article with fewer crisp entity references and fewer answer-shaped sections. That's a style shift that may be visible to other definitional queries too.

---

## 5. Per-dim regressions exceeding the 10% tolerance

The bench script exits `rc=1` if any per-dim relative drop exceeds the `--tolerance` threshold (default 0.10). Four dims cross that threshold:

| # | keyword | dimension | weight | opus score | sonnet score | drop_% | composite impact |
|---|---|---|---:|---:|---:|---:|---:|
| 1 | search-intent | readability_structural | 0.0976 | 0.8078 | 0.7086 | **12.3%** | −0.00968 |
| 2 | search-intent | entity_graph | 0.0366 | 0.1939 | 0.1603 | **17.3%** | −0.00123 |
| 3 | search-intent | entity_coverage | 0.0854 | 0.3670 | 0.3080 | **16.1%** | −0.00504 |
| 4 | seo-for-roofing | entity_coverage | 0.0854 | 0.3448 | 0.2910 | **15.6%** | −0.00459 |

Three of the four (75%) live on a single keyword — `search-intent`. One (seo-for-roofing entity_coverage) is on another keyword. The common dim across both keywords is `entity_coverage` — Sonnet is consistently weaker at explicitly naming canonical entities during a fresh regen.

---

## 6. Timing variability and its implications

One of the most striking findings is how wide the per-keyword timing spread is for Sonnet:

| model | keyword | regen_s | |
|---|---|---:|---|
| Opus | search-intent | 142.2 | min |
| Opus | aeo-vs-seo | 153.4 | |
| Opus | seo-for-roofing | 198.8 | |
| Opus | seo-for-saas | 634.3 | max (4.5× above Opus min) |
| Sonnet | seo-for-saas | 473.6 | min |
| Sonnet | search-intent | 549.5 | |
| Sonnet | aeo-vs-seo | 929.8 | |
| Sonnet | seo-for-roofing | 1063.6 | max (2.2× above Sonnet min) |

- **Opus is cheap-on-short-keywords, expensive-on-long** — the roofing/aeo/intent articles are 800-1400 words, the saas article is ~4000 words, and Opus's regen time correlates with output length as expected.
- **Sonnet does not track output length linearly.** Sonnet is SLOWER on seo-for-roofing (1063s) than on seo-for-saas (473s) despite seo-for-roofing producing a much shorter article. The simplest explanation is higher per-token thinking overhead on definitional/instructional content. The bench captures start/end only, not tokens per second or thinking-versus-output ratios, so this is a hypothesis, not a measurement.
- **Practical consequence**: a Sonnet-based loop would be bottlenecked by whichever keyword's article Sonnet happens to thrash on that iteration. With a 900s regen timeout, seo-for-roofing Sonnet regens would have timed out on THIS bench (1063s > 900s). We'd need to raise the regen timeout to 2000s+ to accommodate Sonnet, which widens the window for stuck-process detection.

---

---

## 7. Outcome

Opus kept for regeneration; Sonnet swap rejected (decision #42). The generator later moved to Gemini Flash for speed (decision #43; see gemini-vs-opus.md).

**Follow-up hypotheses worth testing:**
- Sonnet with lower reasoning effort, to see whether its speed catches up to Opus when thinking tokens are compressed further.
- Other model pairs (e.g. Haiku 4.5 vs Opus 4.6).
- Investigating why Sonnet's regen cost is uncorrelated with article length on this workload (is it re-reading SKILL.md multiple times in a way Opus doesn't?).
