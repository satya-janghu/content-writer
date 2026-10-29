# Gemini 3 Flash / Gemini 3 Pro vs Opus 4.6 (2026-04-16)

**Run window (UTC):** 2026-04-16T07:43:57.378031+00:00 → 2026-04-16T07:54:11.744229+00:00
**Driver:** `scripts/bench_gemini_vs_opus.py` · **Opus reference:** `sonnet-vs-opus.json` · **Note:** this run proposed with `gemini-3-pro-preview`; production uses `gemini-3.1-pro-preview` (a distinct model ID confirmed by API probe), whose observed propose max was 211 s.
**Cache epoch:** `v3.7-no-judge` (10-dim composite, `n/82` weights)
**Models under test:** Flash=`gemini-3-flash-preview`, Pro=`gemini-3-pro-preview`
**Tolerance:** 10% per-dim relative drop



## 1. Executive summary

| | **Opus 4.6** | **Gemini 3 Flash** | Ratio / Δ |
|---|---:|---:|---:|
| Avg regen time | 282.2s | 142.6s | 0.51× |
| Avg composite | 0.8394 | 0.8406 | +0.0012 |
| Max regen time | 634.3s | 242.2s | — |
| Min regen time | 142.2s | 44.4s | — |
| Keywords where Flash ≥ Opus composite | — | 2/4 | — |


## 2. Event timeline

| # | Keyword | Model | Phase | Elapsed |
|---|---|---|---|---:|
| 1 | seo-for-roofing | Flash | REGEN | 242.2s |
| 2 | seo-for-saas | Flash | REGEN | 46.4s |
| 3 | aeo-vs-seo | Flash | REGEN | 44.4s |
| 4 | search-intent | Flash | REGEN | 237.2s |
| 5 | seo-for-roofing | Pro | PROPOSE | 172.4s |
| 6 | seo-for-saas | Pro | PROPOSE | 144.5s |
| 7 | aeo-vs-seo | Pro | PROPOSE | 137.9s |
| 8 | search-intent | Pro | PROPOSE | 118.7s |

## 3. Per-keyword headline table

| keyword | opus_comp | opus_regen_s | flash_comp | flash_regen_s | Δcomp | speed_ratio |
|---|---:|---:|---:|---:|---:|---:|
| seo-for-roofing | 0.8258 | 198.8 | 0.8022 | 242.2 | -0.0235 | 1.22× |
| seo-for-saas | 0.8543 | 634.3 | 0.8225 | 46.4 | -0.0319 | 0.07× |
| aeo-vs-seo | 0.8452 | 153.4 | 0.8843 | 44.4 | +0.0392 | 0.29× |
| search-intent | 0.8323 | 142.2 | 0.8534 | 237.2 | +0.0211 | 1.67× |

### 4.1 seo-for-roofing

- Opus regen: **198.8s** → composite **0.8258**
- Flash regen: **242.2s** → composite **0.8022**
- Composite Δ: **-0.0235**

| dim | weight | opus | flash | Δraw | Δrel_% | Δcontrib | flag |
|---|---:|---:|---:|---:|---:|---:|:-:|
| topical_coverage | 0.3049 | 1.0000 | 1.0000 | +0.0000 | +0.0 | +0.00000 | = |
| corpus_match | 0.1341 | 0.7755 | 0.9293 | +0.1538 | -19.8 | +0.02064 | ⬆ |
| keyword_relevance | 0.1220 | 0.8743 | 0.8797 | +0.0054 | -0.6 | +0.00066 | ⬆ |
| readability_structural | 0.0976 | 0.7936 | 0.8095 | +0.0159 | -2.0 | +0.00155 | ⬆ |
| entity_coverage | 0.0854 | 0.3448 | 0.1410 | -0.2038 | +59.1 | -0.01740 | 🚫 |
| keyword_profile | 0.0610 | 0.6474 | 0.7231 | +0.0757 | -11.7 | +0.00462 | ⬆ |
| content_length | 0.0610 | 1.0000 | 0.4451 | -0.5549 | +55.5 | -0.03384 | 🚫 |
| banned_words | 0.0610 | 0.9700 | 1.0000 | +0.0300 | -3.1 | +0.00183 | ⬆ |
| entity_graph | 0.0366 | 0.2006 | 0.1596 | -0.0410 | +20.5 | -0.00150 | 🚫 |
| narrative_coverage | 0.0366 | 0.9963 | 0.9933 | -0.0030 | +0.3 | -0.00011 | ⬇ |
| **total contribution shift** | — | — | — | — | — | **-0.02355** | — |

### 4.2 seo-for-saas

- Opus regen: **634.3s** → composite **0.8543**
- Flash regen: **46.4s** → composite **0.8225**
- Composite Δ: **-0.0319**

| dim | weight | opus | flash | Δraw | Δrel_% | Δcontrib | flag |
|---|---:|---:|---:|---:|---:|---:|:-:|
| topical_coverage | 0.3049 | 1.0000 | 1.0000 | +0.0000 | +0.0 | +0.00000 | = |
| corpus_match | 0.1341 | 0.9472 | 0.9662 | +0.0190 | -2.0 | +0.00254 | ⬆ |
| keyword_relevance | 0.1220 | 0.8734 | 0.8747 | +0.0013 | -0.1 | +0.00016 | ⬆ |
| readability_structural | 0.0976 | 0.9239 | 0.9130 | -0.0109 | +1.2 | -0.00106 | ⬇ |
| entity_coverage | 0.0854 | 0.3349 | 0.2553 | -0.0795 | +23.8 | -0.00679 | 🚫 |
| keyword_profile | 0.0610 | 0.6263 | 0.6074 | -0.0189 | +3.0 | -0.00115 | ⬇ |
| content_length | 0.0610 | 1.0000 | 0.6129 | -0.3871 | +38.7 | -0.02360 | 🚫 |
| banned_words | 0.0610 | 0.9400 | 0.9400 | +0.0000 | +0.0 | +0.00000 | = |
| entity_graph | 0.0366 | 0.2251 | 0.1984 | -0.0267 | +11.9 | -0.00098 | 🚫 |
| narrative_coverage | 0.0366 | 0.8865 | 0.8596 | -0.0270 | +3.0 | -0.00099 | ⬇ |
| **total contribution shift** | — | — | — | — | — | **-0.03186** | — |

### 4.3 aeo-vs-seo

- Opus regen: **153.4s** → composite **0.8452**
- Flash regen: **44.4s** → composite **0.8843**
- Composite Δ: **+0.0392**

| dim | weight | opus | flash | Δraw | Δrel_% | Δcontrib | flag |
|---|---:|---:|---:|---:|---:|---:|:-:|
| topical_coverage | 0.3049 | 1.0000 | 1.0000 | +0.0000 | +0.0 | +0.00000 | = |
| corpus_match | 0.1341 | 0.8296 | 0.9718 | +0.1422 | -17.1 | +0.01907 | ⬆ |
| keyword_relevance | 0.1220 | 0.8640 | 0.8765 | +0.0125 | -1.4 | +0.00153 | ⬆ |
| readability_structural | 0.0976 | 0.8636 | 0.9760 | +0.1123 | -13.0 | +0.01096 | ⬆ |
| entity_coverage | 0.0854 | 0.3904 | 0.4515 | +0.0611 | -15.7 | +0.00522 | ⬆ |
| keyword_profile | 0.0610 | 0.6417 | 0.7027 | +0.0610 | -9.5 | +0.00372 | ⬆ |
| content_length | 0.0610 | 0.9973 | 1.0000 | +0.0027 | -0.3 | +0.00016 | ⬆ |
| banned_words | 0.0610 | 0.9700 | 0.9400 | -0.0300 | +3.1 | -0.00183 | ⬇ |
| entity_graph | 0.0366 | 0.2833 | 0.2923 | +0.0090 | -3.2 | +0.00033 | ⬆ |
| narrative_coverage | 0.0366 | 1.0000 | 1.0000 | +0.0000 | +0.0 | +0.00000 | = |
| **total contribution shift** | — | — | — | — | — | **+0.03916** | — |

### 4.4 search-intent

- Opus regen: **142.2s** → composite **0.8323**
- Flash regen: **237.2s** → composite **0.8534**
- Composite Δ: **+0.0211**

| dim | weight | opus | flash | Δraw | Δrel_% | Δcontrib | flag |
|---|---:|---:|---:|---:|---:|---:|:-:|
| topical_coverage | 0.3049 | 1.0000 | 1.0000 | +0.0000 | +0.0 | +0.00000 | = |
| corpus_match | 0.1341 | 0.8415 | 0.9686 | +0.1271 | -15.1 | +0.01706 | ⬆ |
| keyword_relevance | 0.1220 | 0.8679 | 0.8691 | +0.0013 | -0.1 | +0.00016 | ⬆ |
| readability_structural | 0.0976 | 0.8078 | 0.8306 | +0.0227 | -2.8 | +0.00222 | ⬆ |
| entity_coverage | 0.0854 | 0.3670 | 0.2929 | -0.0741 | +20.2 | -0.00632 | 🚫 |
| keyword_profile | 0.0610 | 0.6385 | 0.7144 | +0.0760 | -11.9 | +0.00463 | ⬆ |
| content_length | 0.0610 | 1.0000 | 1.0000 | +0.0000 | +0.0 | +0.00000 | = |
| banned_words | 0.0610 | 0.9400 | 0.9700 | +0.0300 | -3.2 | +0.00183 | ⬆ |
| entity_graph | 0.0366 | 0.1939 | 0.1970 | +0.0031 | -1.6 | +0.00011 | ⬆ |
| narrative_coverage | 0.0366 | 0.9348 | 0.9744 | +0.0396 | -4.2 | +0.00145 | ⬆ |
| **total contribution shift** | — | — | — | — | — | **+0.02113** | — |

## 5. Per-dim regressions exceeding 10% tolerance

| # | keyword | dim | weight | opus | flash | drop_% | composite impact |
|---|---|---|---:|---:|---:|---:|---:|
| 1 | seo-for-roofing | entity_coverage | 0.0854 | 0.3448 | 0.1410 | 59.1% | -0.01740 |
| 2 | seo-for-roofing | content_length | 0.0610 | 1.0000 | 0.4451 | 55.5% | -0.03384 |
| 3 | seo-for-roofing | entity_graph | 0.0366 | 0.2006 | 0.1596 | 20.5% | -0.00150 |
| 4 | seo-for-saas | entity_coverage | 0.0854 | 0.3349 | 0.2553 | 23.8% | -0.00679 |
| 5 | seo-for-saas | content_length | 0.0610 | 1.0000 | 0.6129 | 38.7% | -0.02360 |
| 6 | seo-for-saas | entity_graph | 0.0366 | 0.2251 | 0.1984 | 11.9% | -0.00098 |
| 7 | search-intent | entity_coverage | 0.0854 | 0.3670 | 0.2929 | 20.2% | -0.00632 |

## 6. Timing analysis (regen, 4 samples each)

| model | min | median | max |
|---|---:|---:|---:|
| Opus 4.6 | 142.2s | 176.1s | 634.3s |
| Gemini 3 Flash | 44.4s | 141.8s | 242.2s |

## 7. Pro propose analysis

| keyword | propose_s | edit_status | ±lines | prompt_tok | candidate_tok | thoughts_tok | error |
|---|---:|---|---:|---:|---:|---:|---|
| seo-for-roofing | 172.4 | edit | +6/-6 | 9405 | 5302 | 12603 |  |
| seo-for-saas | 144.5 | edit | +3/-3 | 9433 | 5286 | 14249 |  |
| aeo-vs-seo | 137.9 | edit | +2/-2 | 9400 | 5302 | 13546 |  |
| search-intent | 118.7 | edit | +2/-2 | 9394 | 5292 | 11386 |  |

## 8. Token counts (input + output + thinking)

| task | keyword | prompt_tok | candidate_tok | thoughts_tok | total |
|---|---|---:|---:|---:|---:|
| regen (Flash) | seo-for-roofing | 5464 | 2618 | 62914 | 70996 |
| regen (Flash) | seo-for-saas | 5464 | 4183 | 3006 | 12653 |
| regen (Flash) | aeo-vs-seo | 5464 | 4146 | 2388 | 11998 |
| regen (Flash) | search-intent | 5464 | 2622 | 62910 | 70996 |
| propose (Pro) | seo-for-roofing | 9405 | 5302 | 12603 | 27310 |
| propose (Pro) | seo-for-saas | 9433 | 5286 | 14249 | 28968 |
| propose (Pro) | aeo-vs-seo | 9400 | 5302 | 13546 | 28248 |
| propose (Pro) | search-intent | 9394 | 5292 | 11386 | 26072 |

## 9. Recommendation

- Regen: **Keep regen on Opus 4.6.**
  Flash average regen time = 142.6s (speed ratio vs Opus 0.51×), Flash average composite = 0.8406 (+0.0012 vs Opus), per-dim tolerance breaches = 7, keywords where Flash ≥ Opus composite = 2/4.
- Propose: **Switch propose to Gemini 3 Pro.**
  Pro produced a real edit on 4/4 keywords. Pro max propose time = 172.4s (production ceiling is 1200s).

Heuristics: Flash wins regen only if >10% faster on average AND zero per-dim tolerance breaches AND composite within 0.005 of Opus. Pro wins propose only if it produced real edits on ≥3/4 keywords AND worst-case latency fits the 1200s production ceiling.

**Outcome (decision #43):** propose moved to Gemini Pro. Regen moved to Gemini Flash deliberately for phase 1 (0.51× Opus regen time) despite the 7 per-dim breaches, with a planned phase-2 switch to a stronger generator.
