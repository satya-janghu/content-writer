# Design Decisions

Numbered log of the design decisions behind the current system (cache epoch `v3.7-no-judge`, Gemini generation backend). Numbers are kept from the project's full history; gaps are retired entries.

**Reading guide**: start with #1 (the foundation), then the current-state anchors: #23 (skill architecture), #24 (scoring stack), #33 (no-fallback rule), #35 (v3.4 pooling/depth fixes), #39 (highest-bar benchmarking), #41 (single-pass skill), #42 (v3.7 async loop, judge removed), #43 (Gemini generation backend). Everything else supports these.

## 1. Competitive SERP Scoring over Generic Quality Rubrics

**Decision**: Score content against what's actually ranking for the target keyword, not against an abstract quality rubric.

**Why**: A generic quality rubric (even LLM-judged) is a guess about what Google rewards. The top 3 search results are empirical evidence of what Google is currently rewarding for that specific query. Optimizing toward the competitive profile eliminates the proxy-metric problem — you're measuring against the real outcome.

**Implication**: Every keyword needs 3 competitor URLs as input. The evaluation harness is keyword-specific, not universal.

## 5. Cross-Keyword Rotation to Prevent Overfitting

**Decision**: Optimize the system across 4 keywords in parallel (historically: rotation; current impl: fan-out via `parallel_loop.py`). System-level improvements must pass the per-keyword regression gate on every keyword before they're kept.

**Why**:
- Optimizing against a single keyword's competitive profile risks overfitting — system changes might only work for that specific SERP.
- The 4-keyword gate acts as cross-validation. If a system change helps keyword 1 but hurts keyword 2, it's not a universal improvement.
- This empirically reveals which scoring dimensions actually matter. If improving dimension X consistently helps across all keywords, it's important. If it only helps one keyword, it's SERP-specific noise.

**Implication**: Changes are always system-level (the skill file). No content-level edits (see #21). Configure once; the loop then runs to completion without mid-run input.

## 6. Convergence at N=10 Consecutive Non-Keep Iterations per Keyword

**Decision**: Per keyword, converge after 10 consecutive iterations with no accepted proposal. Convergence is strictly per-keyword; the overall loop exits when all 4 keywords have converged.

**Why**:
- With 10 scoring dimensions, the agent needs enough attempts to explore different improvement directions. Too few (N=5) risks stopping early when there's still room.
- Too many (N=20+) wastes iterations on a genuine plateau.
- Per-keyword convergence ensures each keyword gets sufficient attention independently. Keywords are independent optimization axes — no averaging or ranking across them.

**Implication**: Convergence is a stagnation gate, not a runtime cap. The loop has no hard iteration limit.

## 7. Content Length Is Discovered, Not Prescribed

**Decision**: Content length is not hardcoded. The competitive analysis provides a starting range from competitor word counts, but the loop is free to find the optimal length.

**Why**:
- Content length directly affects multiple scoring dimensions (topic coverage, readability, structural density). Fixing it artificially constrains the optimization.
- The "right" length varies by keyword and SERP. A comparison keyword might reward 3000 words; a definition keyword might reward 800.
- Letting the system discover length means it optimizes holistically — the length that best serves comprehensiveness, readability, and structural quality simultaneously.

**Implication**: Dimension 9 (`content_length`) enforces a soft policy (hard floor = 70% of shortest competitor, soft max = 2× longest, hard cap 7000), not a prescription.

## 14. Topical Coverage as Primary Scoring Signal, Not Word Count

**Decision**: No word-count minimums or maximums drive scoring. The primary target is comprehensive topical coverage across the 3 competitor articles for each keyword. Content length follows naturally from coverage depth.

**Why**: Word-count targets create perverse incentives — padding to hit minimums, cutting substance to stay under maximums. Google rewards topical comprehensiveness, not raw length. Competitor data shows wildly different natural lengths per keyword (aeo-vs-seo: 1,250–1,850 words vs. seo-for-saas: 4,185–7,337 words). A coverage-driven approach self-calibrates length to what the SERP rewards.

**Implementation**: Subtopics are extracted from competitor articles via BERTopic clustering + BGE semantic similarity. Weight by frequency: 3/3 competitors = must-have, 2/3 = important, 1/3 = bonus. Depth factor (top-3 matching sections) rewards sustained treatment rather than passing mention.

**Implication**: Topical coverage is dim 1, weight 25/82 — the highest-weighted dim in the composite.

## 16. Derive Audience from Competitors, Not a Fixed Persona

**Decision**: Audience, vocabulary level, and explanation depth are always derived from competitive analysis. No pre-defined persona exists in the skill.

**Why**: Different keywords target radically different audiences. "what is search intent" targets learners doing a definitional query; "seo for roofing" targets roofers, not SEO directors; "seo for saas" targets SaaS founders. The top-ranking competitors for each keyword reveal the actual audience — their vocabulary, what they define vs. assume, the examples they use. Locking to any single persona would mismatch the SERP for most keywords.

**Implication**: `SKILL.md` derives register (technical, operational, conceptual, commercial) from the topic and bans personas.

## 17. No Related-Content Requirement

**Decision**: The skill does not emit a `relatedContent` frontmatter field or a "Related reading" block at the end of articles.

**Why**: Related content requires existing published articles in the same silo to link to. For a general-purpose content skill, we cannot assume such a silo exists. The related block is a site-specific formatting concern, not a competitive quality signal.

**Implication**: If a downstream project wants cross-linking, it's added as a post-processing step after the skill runs.

## 18. Banned Words as Scoring Penalty, Not Hard Fail

**Decision**: Banned words from the authoritative kill list reduce the composite score rather than causing a hard validation failure.

**Why**: The banned-words list reflects voice preferences, but competitors freely use some of these terms ("hack", "trick", "secret" appear in Backlinko's style). If the loop is optimizing toward competitive content profiles, a hard fail would block convergence; a scoring penalty naturally discourages them without stopping progress.

**Implication**: Dim 10 (`banned_words`) deducts 0.03/hit. The scorer-parseable list lives at `rules/content-generation/banned-words.md`; the same list is inlined in `SKILL.md` for the generator.

## 21. Config-Only Optimization — Never Direct Content Edits

**Decision**: The autoresearch loop ONLY modifies the skill file (`rules/content-generation/SKILL.md`). Content files are ALWAYS regenerated from scratch from the skill. Direct content edits are prohibited.

**Why**: Direct content edits (adding missing terms, restructuring paragraphs, inserting specific phrases) produce keyword-specific artifacts that don't generalize. You end up with one good article that scored well because you manually stuffed it with the right terms, while the underlying generation skill remains unchanged. The next keyword produces the same problems. The goal is a universal content skill — rules that produce competitive output for ANY keyword passed through them.

**The correct loop**: (1) diagnose scoring weakness, (2) modify the rule file to address the root cause universally, (3) regenerate content from scratch from the skill, (4) evaluate, (5) keep or revert the rule change. The content is disposable test output; the rules are the product.

**Implication**: The loop's scope is `rules/content-generation/SKILL.md` only — NOT `content/*.md`. Content files are regenerated as a side effect of each iteration.

## 23. Isolated Content-Generation Skill (No Competitor Access)

**Decision**: The content framework is packaged as a self-contained skill file, `rules/content-generation/SKILL.md`. The skill takes a simple one-sentence prompt ("Write an article about AEO vs SEO") and produces high-ranking content using only that file. It does NOT read competitor content, competitive profiles, or any other project data during generation. Competitors exist only as development-time benchmarks used by `scripts/evaluate.py`.

**Why**: In production the skill is invoked on any topic, and callers won't supply competitor URLs or benchmark profiles. For the skill to be universal and reusable, generation must work purely from internal tuned rules. Earlier attempts had the skill reading competitor content as generation input, producing keyword-specific behavior that didn't generalize. The 4 test keywords are development-time benchmarks only; once tuning is complete, the benchmarks are irrelevant to the skill's operation.

**Architecture**:
- Skill: `rules/content-generation/SKILL.md` (single file since #41)
- Scorer data: `rules/content-generation/banned-words.md`
- Autoresearch scope: `SKILL.md` only

**Porting**: Copy `SKILL.md` into any agent or LLM workflow that loads markdown instructions; it needs no other files.

## 24. Harness v3.0 — SOTA Model Stack for Scoring

**Decision**: Use a SOTA model stack for the evaluation harness: BAAI/bge-large-en-v1.5 for embeddings, BAAI/bge-reranker-v2-m3 for paragraph relevance, urchade/gliner_large-v2.1 for zero-shot NER with custom entity types, BERTopic for interpretable topic modeling, facebook/bart-large-cnn for abstractive summaries, KeyBERT for BERT-based keyword extraction, NetworkX for entity co-occurrence graphs, rank-bm25 for lexical matching (top competitor terms as query, content as document), and Google Gemini for LLM-judge scoring of factual accuracy / coherence / originality (the judge has been dormant since #42). 11 scoring dimensions at the time (10 since #42; weights in docs/ARCHITECTURE.md).

**Why**: The initial lightweight stack had measurable gaps. mpnet's 768-dim embeddings are outperformed by BGE-large's 1024-dim for English semantic retrieval. spaCy's fixed entity types miss domain-specific entities (SEO tools, AI assistants, metrics) that GLiNER's zero-shot custom types catch. HDBSCAN alone gave clusters without interpretable topic terms, which BERTopic provides. BM25 applied properly (short query against content as document) is a distinct signal from TF-IDF cosine. BART summaries enable narrative-level gap detection that term-level overlap misses. LLM-as-judge is the only way to reach factual/originality/coherence dimensions; small non-determinism (~1–2% despite temperature=0.0) is accepted as the cost of that signal. Neo4j was considered and rejected — persistent graph DB overkill; in-memory NetworkX is right-sized.

**Gemini model**: see #28 (v3.2 swap to Gemini 3) and #30 (correct model ID `gemini-3-flash-preview` + 30s ThreadPoolExecutor hard-timeout wrapper). API key lives in gitignored `.env`.

**Implication**: Harness requires ~4 GB of model downloads on first run. Peak RAM ~4–5 GB. One-shot installer at `scripts/install_deps.sh`. Profile JSONs grow from ~15 KB (legacy) to ~150–600 KB due to stored summaries, KeyBERT terms, entity graphs, cluster centroids. Computing profiles once is cheap; loop iteration cost is dominated by the per-iteration `evaluate.py` call.

## 25. Single Generation per Iteration — Per-Competitor Benchmarking Replaces Stochasticity Hedge

**Decision**: Generate content once per iteration and trust the per-competitor benchmarking signal. No duplicate-generation averaging.

**Why**: Observed noise between duplicate generations was 0.0006 (on a 0–1 scale) — negligible. The multi-dimensional scoring with per-competitor win-rate logic (for readability, structural, entity_coverage) and comparison against each of the 3 competitors individually produces a much stronger signal than averaging two runs. Duplicate generation doubled LLM cost per iteration without meaningful signal improvement.

**Implication**: The per-keyword gate is the over-fitting safety net; #42 explains why the cross-keyword re-check was retired.

## 26. Fedora Port + GPU Best-Config Pass — Pinned Deps, Token-Aware Chunking, Per-Model Flush

**Decision**: Run on Fedora with NVIDIA RTX 2070 Super (8 GB VRAM). Pin `transformers<5` and `sentence-transformers<5`. Run BART/GLiNER/BGE/reranker on GPU with explicit best-config (fp16, `truncation=True` on the BART pipeline, GLiNER `quantize.compile` when available, `batch_size=32` on every encode/predict, batched BART chunk-summarize). Use a sentence-aware token-bounded GLiNER chunker that subtracts the entity-prompt prefix from the 384-token cap. Add `torch.cuda.synchronize()` + `empty_cache()` between every model handoff per competitor. Wrap Gemini calls with retry+rate-limit. Bound `KeyBERT` to `CountVectorizer(ngram_range=(1,3), stop_words="english", max_features=2000)`.

**Why**: macOS BART silently CPU-truncates oversized inputs; the same code on CUDA Linux dispatches to a kernel that asserts on out-of-range position-embedding indices, poisoning the CUDA context for all subsequent model calls in the process. transformers 5.x dropped the `"summarization"` task name from `pipeline()`, so BART fails to load entirely on the latest pip pull. The default `KeyBERT` candidate space (tens of thousands of n-grams on a 30 KB doc) makes CPU vectorization unworkable in this hot loop. The 384-token GLiNER cap measures *processed* length (entity-prompt prefix + input + special tokens); a chunker that measures input only consistently overflows on dense docs. Per-model `synchronize+empty_cache` surfaces async errors at the failing model rather than at iteration boundaries.

**Implication**: `requirements.txt` pins `transformers<5` and `sentence-transformers<5`. BERTopic uses CPU UMAP + HDBSCAN, which is fast at these document counts and keeps cupy out of the shared CUDA context.

## 27. BART Tokenizer Length Pinning — `tokenizer.model_max_length` Set Explicitly

**Decision**: Explicitly pin `SUMMARIZER.tokenizer.model_max_length = 1024` immediately after constructing every `facebook/bart-large-cnn` pipeline. Applied in both `scripts/analyze_competitors.py` and `scripts/evaluate.py`.

**Why**: `bart-large-cnn` ships with `tokenizer.model_max_length` set to the HuggingFace sentinel `1000000000000000019884624838656` (meaning "unset"). With `truncation=True` and no explicit `max_length`, the tokenizer truncates to that sentinel, i.e. never. The 800-word chunker in both scripts can produce chunks up to about 1052 tokens on dense competitor text, while BART's learned position embeddings are sized 1024. Pinning the length keeps every chunk inside the 1024-position limit on both CPU and CUDA.

**Implication**: Profile rebuild runs end-to-end on GPU in ~2m50s; the four-keyword baseline takes ~2m45s including model load.

## 28. v3.2 Baseline Reset — Gemini 3 Flash + Autonomous Run Harness

> Partly superseded: #33 removed the neutral-0.5 fallback, #30 corrected the model ID, and #42 retired `--batch-timeout`.
>
> Detail on #33 (2026-04-15): the "graceful 0.5-neutral fallback" described below was removed. Every scorer now either returns a real number or raises `ScoringError` — iteration is dropped, no fabricated scores. The model ID was also corrected in #30 (`gemini-3.0-flash` was invalid; the real ID is `gemini-3-flash-preview`) and the outer retry count bumped to 3 with exponential backoff in #33. Everything else in this entry (`weights_epoch`, `--batch-timeout` fuse, `run_until_done.sh`, `state.json.baselines`) still stands.

**Decision**: Swap the LLM judge from Gemini 2.5-flash to Gemini 3 Flash (scoring-semantic change), move baselines from a hardcoded `load_baselines()` dict into a `state.json.baselines` block populated by a one-time `--establish-baselines` run, add a per-Gemini-call timeout, add a `--batch-timeout` fuse that exits with code 4 so an outer wrapper can restart via `--resume`, and build `scripts/run_until_done.sh` as the autonomous-run entry point with exponential-backoff restart on every non-fatal exit.

**Why**: Three forcing functions.

(1) Gemini 2.5-flash occasionally stalled inside `httpcore._receive_response_headers` for minutes at a time, wedging the daemon. A per-call timeout means a single stuck request can cost at most `batch_timeout` seconds before the outer wrapper restarts from a clean daemon.

(2) Hardcoded baselines in `load_baselines()` quietly coupled the driver to a specific harness version. Moving them to `state.json.baselines` + `--establish-baselines` decouples scoring config from code and makes judge-model swaps a one-commit operation (code change, then single `--establish-baselines` run, then resume). The production driver now refuses to start without a baselines block, removing "I forgot to re-baseline" as a silent failure mode.

(3) The loop must run unattended. `run_until_done.sh` wraps `parallel_loop.py --resume`, handles daemon lifecycle, honors the exit-code contract (0 = done, 2 = fatal config, 3/4/other = restartable), and logs structured events to `/tmp/autoresearch-run.log` for post-hoc analysis.

**Implication**: Every rule-change acceptance is gated by the Gemini 3 Flash judge. The convergence gate (per-keyword stagnation) still applies; the definition of "improvement" is just grounded in Gemini 3 scores now.

## 29. v3.3 — GEO/AEO Rule Additions and Scoring Dimensions

> **Scoring portion superseded by #37 (2026-04-15):** the two GEO scoring dimensions (12 `geo_structural_readiness`, 13 `geo_visibility_sim`) were removed from the composite. The **GEO rule files and generation-time pipeline wiring added by this decision remain in force** — `geo-optimization.md` is still read in Phase 2; `structural-rules.md`'s 150–300-word paragraph target and `humanize-formatting.md`'s 5–10% bold budget still apply. Only the *scorer* stopped measuring compliance. Read this entry for the research basis behind those rules; read #37 for why scoring was dropped. Since #41 these rules live in the GEO section of `SKILL.md`; the separate rule files named below no longer exist.

**Decision**: Add a universal rule file `references/geo-optimization.md` anchored to Yu et al. (GEO-SFE 2026), Aggarwal et al. (KDD '24), Lee (2026 replication), and Chen et al. (2025). Tighten `structural-rules.md` paragraph density (from "3–5 sentences" to "150–300 words") and `humanize-formatting.md` bold budget (from "one or two per section" to "5–10% of body word count" with the AI-tell guardrail preserved). Originally added two scoring dims (deterministic GEO Structural Readiness + Gemini-judged GEO Visibility Simulation) — both subsequently removed in #37.

**Why**: Chen et al. (2025) established that Google SERP rank and AI-search citation correlate ρ ≈ 0 on 19,556 queries. The existing composite optimized entirely for SERP-style competitive parity; nothing shaped the article to be cited by a generative engine. Yu et al. isolated a 17.3% citation-rate lift from structural changes alone. Bringing those structural targets (stats density, claim-evidence ratio, bold coverage, 150–300-word paragraphs) into the skill's rule files directly shapes generation for AI citation.

**Why rules retained after scoring removed**: the rules still shape Phase 2 of generation so content remains GEO-optimized at write-time. What the scoring loop stopped doing (in #37) was gating rule changes on GEO compliance — because the scoring loop has no SERP-ranked reference for GEO, and the compliance gap (#36) cannot be closed by rule tightening in any case.

**GEO vs SEO/AEO conflict resolution**: the table at the bottom of `geo-optimization.md` is authoritative when the frameworks disagree (e.g. paragraph length: SEO "short scannable" vs. GEO 150–300 words — GEO wins because Chen et al. find SEO rank doesn't predict GEO citation).

## 30. Gemini Judge Model ID Correction and ThreadPoolExecutor Timeout Wrapper

> **Partially superseded by #33 (2026-04-15):** the outer retry loop was restored from 2 → 3 attempts with explicit 3 / 9 / 27 s exponential backoff, and the path on exhaustion changed from returning `(None, reason)` to raising `ScoringError("gemini", reason)`. The model-ID correction, the `ThreadPoolExecutor(30s)` wrapper, and the in-SDK `HttpRetryOptions` still stand.

**Decision**: Pin the judge model ID to `gemini-3-flash-preview` (not `gemini-3.0-flash`, which is not a real Gemini API identifier and returns 404 on every call). Wrap every `generate_content` call in a fresh `concurrent.futures.ThreadPoolExecutor(max_workers=1)` with `future.result(timeout=30)` — a hard wall-clock ceiling the SDK cannot bypass. Configure the SDK's `HttpRetryOptions(attempts=3, exp_base=2, initial_delay=2, jitter=0.3, max_delay=20, http_status_codes=[429, 500, 502, 503, 504])` for in-SDK backoff on transient 5xx/quota. Short-circuit on 404 (terminal config error, not a flake).

**Why — two changes:**

(1) **Model ID.** Use `gemini-3-flash-preview` (hyphen-separated, `-preview` suffix). A 404 is treated as terminal via the `is_404` short-circuit.

(2) **Timeouts.** The google-genai client does not enforce `http_options.timeout` (it passes `timeout=None` to its internal httpx client). A ThreadPoolExecutor wrapper enforces a hard ceiling with `future.result(timeout=N)` at the Python level. On timeout, `executor.shutdown(wait=False, cancel_futures=True)` returns immediately; the underlying thread keeps running until the OS drops the TCP connection (seconds to low minutes). Thread leakage is bounded in practice; over an hours-long run we may leak ~1–3 threads total.

30s per attempt accommodates long-tail server latency with headroom; real Gemini 3 Flash calls on our prompt sizes complete in <5s.

**Implication**: Baselines can be established meaningfully.

## 31. Skill H1 Contract Fix — In-Body `# H1` Mandatory, Frontmatter `title:` Is Not an H1

**Decision**: Every generated article emits an in-body markdown `# H1` heading immediately after the YAML frontmatter close. Frontmatter `title:` is the HTML `<title>` / SEO meta-title and is documented as a separate field. Enforced in `SKILL.md` (frontmatter template and Structure section).

**Why**:

1. **Markdown semantics**: YAML frontmatter `title:` is a metadata field whose downstream role is the HTML `<title>` element / social-card title / SEO meta-title — it is not a markdown H1. Static site generators, markdown-to-HTML converters, and readers all treat them as separate concerns. The evaluator (`_count_headings_by_level` in `scripts/evaluate.py`) correctly parses only in-body `#`-prefixed markers; the skill was the broken side of the contract.

2. **Boundary risk**: letting the autoresearch proposer discover and "fix" this was not safe. The proposer could have proposed a scorer-side change ("count frontmatter `title:` as H1"), which crosses the immutable boundary between "improve the skill" and "game the scorer". Fixing the contract at the source eliminates the degree of freedom.

**Lesson**: when a scorer sub-score produces an unexpected penalty, the default frame should be "what contract did the content/skill break?" before "what headroom does this represent?"

## 33. No Neutral-0.5 Fallbacks Anywhere — Scorers Raise Or Succeed

**Decision** (2026-04-15): Every scoring dimension either returns a real number in [0, 1] or raises `evaluate.ScoringError(dim, reason)`. The iteration-worker catches, logs, and drops the iteration (rc=4); the merge queue sees one fewer proposal; the loop continues. No fabricated quality signals anywhere.

**Why**: the neutral-0.5 pattern looked defensible — "keep the loop alive when infrastructure flakes" — but it silently corrupts the per-keyword gate. A single transient Gemini timeout drops a dim's score from ≈ 0.93 to 0.5, a composite hit of `~0.02`. The strict `>=` gate then rejects the proposal as a "regression" even though the rule change was fine — the score just reflected a bad API roll. The right abstraction is *fail the dim*, not *make up a number*. Infrastructure errors are not quality signals.

**Scope by failure bucket**:

| Bucket | Behaviour |
|---|---|
| Config error (profile missing field) | `raise ScoringError(dim, "re-run analyze_competitors.py")` — fix the config |
| Model error (local GPU/CPU) | `raise ScoringError(dim, f"{model} failed: {e}") from e` — fix GPU/infra |
| Transient Gemini (timeout, quota, non-JSON) | Retry 3× with exponential backoff (3 s, 9 s, 27 s) inside `_gemini_generate_json`; raise after exhaustion |
| Terminal Gemini (404 wrong model ID) | Short-circuit immediately, raise |
| Data-shape (empty content edges, 0 valid queries) | `raise ScoringError(dim, "...")` — content/profile malformed |

**Propagation path**: scorer raises `ScoringError` → `evaluate_content` propagates → `eval_server` catches in its generic handler and returns `{"ok": False, "error": ..., "traceback": ...}` over the socket → `eval_client.score_article` raises `DaemonUnavailable("daemon refused: [dim] reason")` → `iter_worker` catches and returns rc=4 → `parallel_loop` logs the worker rc and drops the proposal.

**`_gemini_generate_json` signature**: `(prompt, log_tag=...) -> dict | list` (was `tuple[dict | list | None, str | None]`). The tuple signature pre-dated this decision and its `None` half was the mechanism by which fallbacks propagated; removing it kills the pattern at the source.

**Config-error posture**: hitting a config error halts the loop on that keyword. That is the correct response — a profile without required fields should not score at all until someone re-runs `analyze_competitors.py`. Loud failure + operator fix > silent degradation.

## 34. Proposer Target Selection — Headroom `(1 - score) × weight`, Not Raw-Min Score

**Decision** (2026-04-15): `_weakest_dim_report` selects `weakest_dim` by **max composite headroom** — `(1 - score) × weight` — with raw-min score as the tiebreaker and alphabetical name as the final tiebreaker. The raw-min selector is preserved only as a secondary diagnostic field (`raw_weakest_dim`, `raw_weakest_score`).

**Why**: raw-min selection pointed the proposer at low-weight dimensions where even a perfect fix barely moved the composite. A dim at score 0.30 with weight 0.03 contributes at most `0.021` of additional composite even if lifted to 1.0, while a dim at score 0.55 with weight 0.25 contributes up to `0.1125`. The raw-min selector chased the first; the loop then either produced a rule edit the composite couldn't feel, or the proposer returned "no edit" because the target was obviously not worth pursuing.

**Ceiling-lock property**: any dim at exactly 1.0 has `headroom = 0`. Under the new selector a ceiling-locked dim (e.g. topical_coverage on all four keywords today) can never be picked unless every other dim is also at 1.0. This matches the intent that ceiling-locked dims should not be perpetual proposer targets.

**Non-destructive migration**: every field present before this change is still present with the same semantics. `weakest_dim` is the only field whose *value* changes behaviourally, and it now points somewhere more useful.

## 35. v3.4 Scoring-Resolution Fixes — Pooling and Depth (P1–P4)

**Decision** (2026-04-15): four scoring-resolution fixes targeting dimensions that couldn't distinguish "adequate" from "excellent" content, giving the proposer more gradient to climb. P5 (per-section narrative summaries + Hungarian matching) is deferred until P1–P4 validate in the loop.

**Why**: the pre-fix composite had five ceiling-prone dims. Under pure mean across per-competitor scores, a content piece could match 3/4 competitors strongly and miss the fourth entirely with no score penalty. Binary substring checks couldn't tell a passing mention from a 200-word section. A `* 1.5` saturation hack on node_overlap zeroed out reward past 67% coverage. Corpus-match normalization anchored the ceiling to "the average competitor," not "the most corpus-coherent competitor." Each of these caps was a *structural* blindness in the scorer — no rule change the proposer could make would move a saturated dim.

**Scope**:

| # | Dim | Change | Before | After |
|---|-----|--------|--------|-------|
| P1 | `narrative_coverage` | Aggregation of per-competitor BGE cosines | `mean(sims)` | `0.6 * mean + 0.4 * min` via `_mean_min_blend()` |
| P1 | `keyword_profile` | Aggregation of per-competitor best-match KeyBERT cosines | `mean(per_comp_scores)` | `0.6 * mean + 0.4 * min` |
| P2 | `entity_coverage` | `canonical_overlap` sub-score | `hits / len(canonical)` (binary `ent in content`) | `mean(min(1, count(ent)/3))` across canonical entities — depth-graded |
| P3 | `entity_graph` | `node_overlap` term | `0.5 * min(1.0, node_overlap * 1.5)` — saturated at 67% | `0.5 * node_overlap` — full range rewarded |
| P4 | `corpus_match` | TF-IDF + BGE cross-reference normalizer | `mean(comp_cross)` / `mean(sem_cross)` | `max(comp_cross)` / `max(sem_cross)` — ceiling = most corpus-coherent competitor |

**Mean-min blend rationale (P1)**: `0.6 * mean + 0.4 * min`. Pure `max` would ignore every non-best competitor (one-source optimisation). Pure `min` would over-punish a single-competitor outlier. 0.6/0.4 keeps mean as primary signal but makes the worst-case gap visible — a piece matching 3/4 competitors at 0.9 and one at 0.4 blends to `0.6*0.775 + 0.4*0.4 = 0.625`, down from 0.775 under pure mean; a uniform-0.775 piece still scores 0.775.

**Depth factor (P2)**: `min(1.0, count(ent) / 3.0)` per canonical entity — 1 mention → 0.33, 2 → 0.67, 3+ → 1.0. Saturation at 3 prevents a single entity from dominating through keyword-stuffing.

**Node-overlap unsaturation (P3)**: the prior `min(1.0, node_overlap * 1.5)` meant 67% hit-rate produced an identical contribution to 100%. The linear `node_overlap` now differentiates across the full range.

**Ref-sim max (P4)**: `max(comp_cross)` and `max(sem_cross)` shift the corpus-match ceiling from "average competitor coherence" to "best competitor coherence". Clamped to `min(1.0, ...)` to keep the score in [0, 1] when content exceeds the best competitor. The detail payload exposes `ref_lex_max` and `ref_sem_max` for diagnostics.

**Not in scope**:
- `corpus_match` BM25 leg (different normalizer structure — `mean(competitor_bm25_query_scores)`).
- `topical_coverage` (still ceiling-locked at 1.0 on all 4 keywords; a separate recipe issue, not a pooling problem).
- P5 (per-section BART summaries + Hungarian matching on narrative_coverage) — deferred until P1–P4 validate.

## 36. GEO Dimensions: Generation Compliance, Not Rule Edits

**Decision** (2026-04-15): GEO-related weaknesses (for example a bold-emphasis ratio of 0.0 against a rule that mandates 5-10%) are a generation-compliance matter rather than a rule-content matter, because the rules already demand what the GEO dims measure, so no rule edit can move them. That reasoning drove #37 (stop scoring GEO), #38 (full-context proposer) and #41 (single-file skill). The entry is kept because `_propose_prompt.py` and `evaluate.py` reference #36.

## 37. v3.5 — GEO Dimensions Removed from Scoring; Rules Kept in Generation

**Decision** (2026-04-15): the autoresearch scoring composite drops dim 12 (`geo_structural_readiness`) and dim 13 (`geo_visibility_sim`). The remaining 11 SERP dims absorb the freed 0.10 weight proportionally (every weight scales by `1 / 0.9`, stored as `n / 90` fractions in `evaluate.WEIGHTS` to keep ratios exact). **GEO rules stay in the skill** and still shape generation. Only the *scorer* stopped measuring compliance with them.

**Why**: the scoring loop only gates against dims where we have SERP-ranked reference content to benchmark against. The two GEO dims used *absolute research-derived targets* (Aggarwal KDD'24, Lee 2026, GEO-SFE 2026) — not competitor-derived — because the published GEO research explicitly warned that SERP-ranked content was a poor reference for generative-engine visibility (Chen et al. 2025: ρ ≈ 0 on 19k queries). We accepted that argument in v3.3 (#29) and added the dims anyway. The practical consequence (#36): when the rules already mandate the measured floors, the ratchet needs a *regeneration-pipeline compliance fix*, not a rule tightening, so a rule-edit loop cannot improve those dims.

Keeping the GEO rule files in the skill captures the optimisation intent — Phase 2 still tells the model to anchor every major claim to a statistic, bold 5–10% of body words, follow claim→evidence→implication, etc. The content is still GEO-optimised at generation time. We are *not* scoring GEO compliance in the loop, because (a) we don't have ranked reference content that validates the specific absolute targets as rank-causal, and (b) the loop's mechanism (rule-edit → regen → score ratchet) doesn't reach the generation-compliance gap.

**Scope**:

| Area | Before (v3.4) | After (v3.5) |
|---|---|---|
| `evaluate.WEIGHTS` | 13 dims, weights sum 1.0, GEO dims at 0.05 each | 11 dims, `n / 90` fractions, sum 1.0 exactly |
| `evaluate_content` | Calls all 13 `run(...)` entries | Calls 11; GEO `run` lines deleted with an explanatory comment pointing at this decision |
| `score_geo_structural_readiness`, `score_geo_visibility_simulation` | Called by evaluator | **Retained in code, unreferenced.** Kept so a future decision can re-enable with `git diff` as the only intervention |
| `scripts/_propose_prompt.py` | Named GEO dims and `geo-optimization.md` as preferred edit target | Generalised — any dim emitting `sub_scores` still routes the proposer at the lowest sub-signal, no GEO-specific naming |
| `scripts/score_cache.py` | `weights_epoch = "v3.4-pooling-depth"` | `weights_epoch = "v3.5-no-geo-scoring"` |
| `autoresearch-state.json.baselines` | v3.4 numbers | v3.5 numbers; `baselines_meta.source = "decisions-37-remove-geo-scoring"` |
| Profile JSONs (`geo_sim_queries`) | Used by `score_geo_visibility_simulation` | **Left in place.** Inert now; removing would require re-running `analyze_competitors.py` on every other profile field too |
| Pipeline ordering | Phase 2 applies GEO rules before humanize/AEO/voice | **Unchanged.** |

**Weakest-dim selector after removal**: the headroom selector picks `entity_coverage` on every keyword (headroom 0.029–0.036). The consistency is useful signal — every keyword's next-most-improvable dim is now the same mid-weight SERP dim, so a rule edit that moves `entity_coverage` universally would land across all 4 gates at once. Whether that's actionable through rule tightening (vs. the compliance-gap pattern from #36) is the next open question.

**Reinstatement path**: if a later decision re-enables GEO scoring, the changes are small — put the two `run(...)` calls back in `evaluate_content`, restore the WEIGHTS entries, bump the epoch, re-baseline. The `score_geo_*` functions remain callable; the `geo_sim_queries` profile field remains populated.

## 38. Proposer Realigned with Autoresearch Design — Agent Does Ideation, Not Just Modification

**Decision** (2026-04-15): `scripts/_propose_prompt.py` rewritten to give the proposer agent the full per-dim score breakdown, one-line scorer semantics for every dim, per-dim measurement details, git-log history, and the `autoresearch-results.tsv` tail — instead of pre-selecting one "weakest dimension" in Python and handing the agent a single target. Earlier revisions collapsed the autoresearch "Ideate" phase into Python (`evaluate._weakest_dim_report` picked the highest-headroom dim; the propose prompt framed the entire instruction around "improve dim X"), which limited the proposer to a single pre-selected target: when the chosen dim's surface concept is already named in the rules, the agent concludes "the rule already addresses this" and exits with no edit. The agent has no view of *other* weak dims and no permission to switch targets, so perfectly real levers elsewhere go unaddressed.

**Why it matters**: the autoresearch design (Karpathy's autoresearch pattern) puts the LLM in charge of BOTH Phase 2 (Ideate — pick the next experiment) and Phase 3 (Modify — make ONE focused change). Pre-targeting in Python demotes the agent to Phase 3 only.

**Scope**:

| File | Change |
|---|---|
| `scripts/_propose_prompt.py` | Full rewrite. Renders: per-dim score table sorted by headroom (with one-line scorer semantics for each of the 11 dims), per-dim measurement detail blocks, recent git log, `autoresearch-results.tsv` tail, explicit "make the edit, don't describe it" framing, "no edit is almost never the right answer" guard. Simplified again 2026-04-15 (realign pass): dropped over-prescriptive "How to think (autoresearch protocol)" 5-step section in favour of trusting the agent to reason. |
| `scripts/evaluate.py` | `_weakest_dim_report` now includes full `details` dict (additive — `weakest_*` legacy fields preserved). |
| `scripts/iter_worker.py` | Unchanged signature; passes the same `_weakest_dim_report` output to `propose.propose`. The report now carries enough data for the agent to do its own ideation. |
| `scripts/propose.py` | Per-invocation logging of the proposer's response so a "no edit" outcome is diagnosable post hoc. |

**Lesson**: when an autoresearch-style loop's per-iteration agent is a fresh subprocess (no in-process memory), the prompt MUST surface git log + results log explicitly. The subprocess model means we pay this cost every iteration; the prompt is the only memory channel.

## 39. v3.6 — Highest-Bar Benchmarking Per Dimension

**Decision** (2026-04-15): lift three scoring dimensions' competitor-pooling to the "highest bar" principle — for coverage-type dims, use the union of all competitor signal; for quality-type dims, use the best individual competitor's score. Baselines dropped uniformly 0.01–0.03 across all four keywords, confirming the bar rose. Cache epoch bumped `v3.5-no-geo-scoring → v3.6-highest-bar`; baselines re-established.

**Why**: the autoresearch loop is gated against competitor content. When that gate is set to "match the average competitor" (mean-pooled), there is much less headroom for the proposer to exploit than when it's set to "match the best competitor" (max-pooled) or "cover everything any competitor covers" (union). The three dims below were still pooling at something less than highest-bar; fixing them expands proposer headroom without changing the scorer's fundamental math.

**Scope**:

| Dim | Before | After |
|---|---|---|
| `corpus_match` BM25 leg | `ref_bm25 = mean(comp_bm25)` on the top-terms query | `ref_bm25 = max(comp_bm25)` — mirrors the TF-IDF/BGE legs' v3.4 P4 fix (decisions #35) |
| `entity_coverage` canonical set | Entities mentioned by ≥ 2 competitors (intersection-leaning) | Union — every entity mentioned by any competitor. Depth-graded averaging unchanged |
| `entity_coverage` per-competitor overlap | `mean(overlaps) * 2.0` saturated at ratio 0.5 | `max(overlaps)` — match the most entity-rich competitor's Jaccard coverage |
| `entity_coverage` combined weight mix | `0.55 * canonical + 0.25 * mean_overlap * 2.0 + 0.20 * win` | `0.60 * canonical + 0.25 * max_overlap + 0.15 * win` |
| `readability_structural` structural sub-scores | `win_rate_score(content, comp_values)` on heading count + answer-block ratio (0/0.5/0.75/1.0 stepping) | `content / max(comp_values)` clipped to [0, 1] — full credit only at the top competitor's level |

**Dims left alone**:

- `topical_coverage` — already uses BERTopic clusters extracted from the competitor corpus union; no change.
- `keyword_profile` + `narrative_coverage` — mean+min blend (`0.6 * mean + 0.4 * min`) IS the higher bar for similarity-pooled dims. Switching to `max` would encode "match one competitor well" (lower bar); the blend encodes "match everyone, get penalised for any gap." Left alone.
- `corpus_match` TF-IDF + BGE legs — already `max`-normalised (v3.4 P4).
- `entity_graph` — already unions competitor edges.
- `keyword_relevance`, `content_length`, `llm_judge`, `banned_words` — standalone metrics, not competitor-benchmarked.

**New baselines under v3.6-highest-bar**:

| Keyword | Baseline |
|---|---|
| seo-for-roofing | 0.8457 |
| seo-for-saas | 0.8341 |
| aeo-vs-seo | 0.8628 |
| what is search intent | 0.8521 |

Drop from v3.5 was uniform 0.01–0.03, confirming the lift is real and measurable but not pathological. Lowest composite is now seo-for-saas at 0.8341 — this is the keyword the loop has the most headroom to act on first.

## 41. Single-Pass Skill Consolidation — One SKILL.md Replaces the Multi-Phase Ruleset

**Decision** (2026-04-16): replace the 4-phase multi-file content-generation skill with a single-pass compressed skill in one consolidated `SKILL.md`. Delete the 13 rule files under `rules/content-generation/` except `banned-words.md` (scorer data). The autoresearch loop now mutates a single target file instead of a tree.

**Why — benchmark-driven**: a three-way benchmark (`docs/benchmarks/skill-architecture-ablation.md`) compared the current multi-phase skill (17,026 words / 15 files / ~800s+ per-article regen) against two alternatives:

- **Single-pass full** (6,151 words, all core rules consolidated into one file, phase scaffolding removed): composite avg 0.8352, 388s parallel for 4 keywords.
- **Compressed** (2,533 words, directives instead of prose rules, guardrails preserved verbatim): composite avg 0.8423, 432s parallel.

Compressed beat single-pass full on average composite despite being 41% of its size, and beat the multi-phase baseline on 2 of 4 keywords (aeo-vs-seo +0.12%, seo-for-roofing +0.27%). The two dims that consistently regressed in both variants were `readability_structural` (−11-12%) and `content_length` (−7 to −11%). `llm_judge` split the variants hard — compressed +0.36%, single-pass full −5.67% — because the single-pass full version included the full content-type template scaffolds and the judge marked template-like prose down on originality.

**Two targeted fixes applied post-benchmark**:

- **Fix A — content length**: added "aim for the upper half of the range by default", explicit "under-length penalized much harder than over-length", and the "articles over 3,000 words should naturally carry 50+ entities" heuristic that implicitly pulls length up. Effect: `content_length` avg 0.8918 → 1.0000 (+12.14%); seo-for-saas went from 3037w / 0.5827 dim score to 4148w / 1.0 dim score.
- **Fix B — structural density**: strengthened "every major H2 must contain ≥1 structured element" from a soft recommendation to a directive, added "every H2 section opens with a direct-answer block (1-3 sentences, or 40-70 words under a 'What is X?' heading)", added "for comprehensive topics aim for 15-25 H3s total" with the H2-breakdown-by-canonical-concern guidance. Effect: asymmetric. Helped aeo-vs-seo (+24.09% on the dim) and seo-for-roofing (+2.27%). Did not move search-intent (persistent weakness, −3.35% on pre-fix). Hurt seo-for-saas (−17.81% on pre-fix) — the rule reshuffles structure differently on long articles.

**Final post-fix baselines** (replace #39 baselines):

| Keyword | Old (v3.6-highest-bar pre-consolidation) | New (post-fix, consolidated skill) | Δ |
|---|---:|---:|---:|
| aeo-vs-seo | 0.8628 | 0.8715 | +1.01% |
| search-intent | 0.8521 | 0.8287 | −2.75% |
| seo-for-roofing | 0.8457 | 0.8597 | +1.66% |
| seo-for-saas | 0.8341 | 0.8465 | +1.49% |
| **AVG** | **0.8487** | **0.8516** | **+0.34%** |

Aggregate composite actually *improved* slightly after the consolidation + fixes, despite the skill being 15% its original size. Three dim/keyword pairs remain >10% below the old multi-phase baseline (search-intent × readability_structural at −33%, seo-for-saas × readability_structural at −16%, seo-for-roofing × entity_graph at −11%) — these are real headroom the loop will work on.

**Scope changes**:

| Area | Change |
|---|---|
| `rules/content-generation/` | Single `SKILL.md` (~2,530 words) plus `banned-words.md` (scorer data) |
| `scripts/_propose_prompt.py` | `RULE_DIR_REL = "rules/content-generation"` (unchanged from #40). Added `SKILL_FILE_REL = "{RULE_DIR_REL}/SKILL.md"`. Proposer prompt rewritten to target one file instead of a tree. |
| `scripts/propose.py` | Docstrings + scope-check comment updated. Uses `RULE_DIR_REL` unchanged (still works as a prefix check). |
| `scripts/iter_worker.py` | `_dirty_paths` prefix remains `rules/content-generation/`. `stub_rule_tweak` now appends to `rules/content-generation/SKILL.md` instead of `structural-rules.md`. |
| `scripts/_regen_prompt.py` | Full rewrite: single-pass prompt pointing at the new `SKILL.md`. All 4-phase language removed. Completion sentinel `<!-- phase-4: pass -->` preserved for harness compatibility (it is the proof-of-completion marker, not a phase indicator anymore). |
| `scripts/bg_commit.py`, `scripts/score_cache.py` | Path references in docstrings updated. |
| `tests/test_banned_words_parser.py` | Path moved to `rules/content-generation/banned-words.md`. |
| `tests/test_iter_worker.py` | `test_stub_rule_tweak` targets `SKILL.md`. |

**What did NOT change**:

- The scorer (`evaluate.py`) — `BANNED_WORDS_PATH` still points at `rules/content-generation/banned-words.md` because the scorer parses that file by section header and the skill's inlined list (for the agent to read) is in prose form.
- The autoresearch loop's gate semantics — per-keyword ≥-baseline regression gate, stagnation streak convergence, parallel-4 worktree fan-out.
- Scoring weights, cache epoch (`v3.6-highest-bar`), or any scoring-semantic decision.

**Regen speed**: serial regen per article is now 350-585s (vs 771-900s+ under the multi-phase pipeline). Parallel 4-way finishes in ~10 minutes for all keywords. Expect loop wall-clock to roughly halve.

**Lesson**: the multi-file rule layout with cross-file duplication was measurably worse for the LLM judge than a single consolidated file. When the same rule appears in 2-3 files saying it slightly differently, the agent hedges — lower bold %, fewer lists, less emphasis — because conflicting-sounding rules produce cautious output. Consolidating into one consistent directive let the agent execute more boldly. For future rule additions: prefer editing SKILL.md in place over creating a new supplementary file.

## 42. v3.7 — Architectural Speedup: Remove Judge, Remove Cross-Keyword Gate, Async Workers; Reject Sonnet Swap

**Decision** (2026-04-16): five coordinated changes aimed at cutting per-iteration wall-clock. Four landed; one — the Sonnet-4.6 model swap — was rejected on empirical bench evidence. The four that shipped together invalidate `v3.6-highest-bar` baselines; cache epoch bumps to `v3.7-no-judge` and baselines re-establish.

### Summary of changes

| # | Change | Shipped? | Reason |
|---|---|:-:|---|
| 1 | Remove `llm_judge` scoring dim | ✅ | 5-7s/call Gemini latency × 16+ scoring calls/batch ≈ 100s/batch overhead; dim is the main source of composite stochasticity (±0.005 drift) which was causing the merge-queue gate to reject real improvements in noise. |
| 2 | Remove cross-keyword merge-queue gate (regen-all-4 + score-all-4 on every keep) | ✅ | Instrumented batch showed gate cost 1290s per keep (55% of wall-clock) and rejected 2/3 of keepers for regressions inside the scoring-noise band. Per-keyword worker-local gate is sufficient proof-of-improvement; the loop self-corrects via iteration if one keyword's evolution hurts another. |
| 3 | Restructure loop as async per-keyword supervisor | ✅ | Instrumented batch showed one worker took 1047s while others finished in 450-792s — 3 workers sat idle 255-597s each waiting for the slowest. Async coordinators remove that penalty. |
| 4 | Swap the generator from Opus 4.6 to Sonnet 4.6 | ❌ | Live bench (`docs/benchmarks/sonnet-vs-opus.md`) rejected: Sonnet was 2.67× slower on average (avg 754s vs 282s) with 4 per-dim regressions >10% tolerance (3 on search-intent), including a −0.007 composite regression on search-intent overall. The plan's speed premise was empirically false. Kept Opus. |
| 5 | Recalibrate timeouts | ✅ (no-op) | Opus observed p95 under v3.7-no-judge is 634s against the current 900s regen ceiling — 1.42× headroom is adequate. Propose 600s likewise fine. No code change; documenting here. |

### Weight redistribution

`llm_judge` contributed 8/90 weight at v3.6-highest-bar. Removed; remaining 10 SERP dims absorbed the freed 0.089 proportionally via `n / 82` fractions. `n` values preserved from v3.6 exactly so ratios between non-judge dims are identical — only the denominator shifted.

| Dim | v3.6-highest-bar weight | v3.7-no-judge weight |
|---|---:|---:|
| topical_coverage | 25/90 ≈ 0.2778 | 25/82 ≈ 0.3049 |
| corpus_match | 11/90 ≈ 0.1222 | 11/82 ≈ 0.1341 |
| keyword_relevance | 10/90 ≈ 0.1111 | 10/82 ≈ 0.1220 |
| readability_structural | 8/90 ≈ 0.0889 | 8/82 ≈ 0.0976 |
| entity_coverage | 7/90 ≈ 0.0778 | 7/82 ≈ 0.0854 |
| keyword_profile | 5/90 ≈ 0.0556 | 5/82 ≈ 0.0610 |
| content_length | 5/90 ≈ 0.0556 | 5/82 ≈ 0.0610 |
| banned_words | 5/90 ≈ 0.0556 | 5/82 ≈ 0.0610 |
| entity_graph | 3/90 ≈ 0.0333 | 3/82 ≈ 0.0366 |
| narrative_coverage | 3/90 ≈ 0.0333 | 3/82 ≈ 0.0366 |
| llm_judge | 8/90 ≈ 0.0889 | **REMOVED** |

`assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9` holds at import in `evaluate.py`.

### Scope changes

| Area | Before (v3.6-highest-bar) | After (v3.7-no-judge) |
|---|---|---|
| `evaluate.WEIGHTS` | 11 keys, `n/90` | 10 keys, `n/82`. `sum(values) == 1.0` assertion preserved. |
| `evaluate_content` | 11 `run(...)` lines including `run("llm_judge", score_llm_judge, body, profile)` | 10 `run(...)` lines; `llm_judge` deleted. `score_llm_judge`, `_gemini_generate_json`, `GEMINI_MODEL` retained as dormant code — one-line reinstatement. |
| `eval_server.warmup()` | Loaded Gemini client into `models_loaded` | Gemini block removed entirely. |
| `eval_server` ops | Included `score_geo_visibility_sim` (Gemini-backed, dormant since #37) | Op removed along with its client wrapper in `eval_client.py`. Daemon now makes zero Gemini calls during scoring. |
| `_propose_prompt.SCORER_SEMANTICS` | 11 entries including `llm_judge` | 10 entries; `llm_judge` removed. |
| `doctor.py` GEMINI_KEY check | Hard check: "the llm_judge dimension needs a valid Gemini key" | Informational only (`ok=True`, hint reworded). |
| `score_cache.MODEL_VERSIONS` | Included `"judge": "gemini-3-flash-preview"` + `weights_epoch: "v3.6-highest-bar"` | `judge` key removed (no longer influences caching); `weights_epoch: "v3.7-no-judge"`. Every cached composite invalidated by the epoch bump. |
| `parallel_loop.drain_merge_queue` | For each keeper: apply → `regenerate_all()` (regen all 4 articles in parallel) → `score_all_via_daemon()` → `per_keyword_gate()` → commit or revert. | For each keeper: apply → commit (real mode) or revert (dry-run). No regen-all, no score-all, no cross-gate. |
| `parallel_loop` main | Batch loop: create 4 worktrees → launch 4 workers → wait for all 4 → drain merge queue → cleanup. `--batch-timeout` fuse wrapped each batch. | Async supervisor: one `worker_coordinator` thread per keyword, one `MergeQueue` drain thread. Coordinators refresh worktree → spawn iter_worker → wait cooperatively on `stop_event` → submit keeper to queue → loop until stagnation/ceiling. Merge thread applies + commits serially. `--batch-timeout` removed. |
| Deleted functions in `parallel_loop.py` | — | `regenerate_all`, `score_all_via_daemon`, `per_keyword_gate`, `run_batch`, `launch_workers`, `wait_for_workers`, `rank_keepers`, `read_proposals_since`, `create_worktrees`, `cleanup_worktrees`, `_mark_batch_start`, `_mark_batch_end`, `_resume_start_iter`, `BatchTimeout`, `_alarm_handler`, `WATCHDOG_ALERT`. |
| Deleted tests | — | `tests/test_gemini_helper.py` (helper is dormant); `tests/test_resume_integration.py` (batch-mode resume semantics don't apply). Batch-specific tests in `tests/test_parallel_loop.py` pruned; the file now covers streak + baseline plumbing only. |
| New tests | — | `tests/test_async_workers.py` — dry-run supervisor converges in <120s end-to-end. |
| `iter_worker.py` | Unchanged. | Unchanged. Still writes one proposal JSON per invocation; the worker-local gate (`post_composite > baseline`) is still the only gate on a keeper. |
| `scripts/run_until_done.sh` | Passed `--batch-timeout 3600` to parallel_loop. | `--batch-timeout` dropped; async mode only produces exit 0 / 2 / crash rc. |

### New baselines under v3.7-no-judge

Re-established 2026-04-16 via `--establish-baselines` against the existing content/*.md files (unchanged; only the composite's dim set and denominator shifted).

| Keyword | v3.6-highest-bar | v3.7-no-judge | Δ |
|---|---:|---:|---:|
| seo-for-roofing | 0.8732 | 0.8499 | −0.0233 |
| seo-for-saas | 0.8715 | 0.8373 | −0.0342 |
| aeo-vs-seo | 0.8742 | 0.8648 | −0.0094 |
| search-intent | 0.8474 | 0.8178 | −0.0296 |

Every baseline dropped because `llm_judge` had been scoring 0.93-0.96 at 8/82 weight; removing it lowered the achievable ceiling. The drop range (0.009-0.034) is consistent with losing a ~0.095 composite contribution. The drop is a denominator artefact, not a quality regression.

### Sonnet vs Opus bench result (see `docs/benchmarks/sonnet-vs-opus.md`)

The benchmark regenerated all 4 keywords with each model under the same `SKILL.md`, scored both via the daemon, compared per-dim. Exit rc=1 (tolerance breach).

| Metric | Opus 4.6 | Sonnet 4.6 |
|---|---:|---:|
| Avg regen time | **282s** | **754s** (+2.67×) |
| p95 regen time | 634s | 1064s |
| Max regen time | 634s | 1064s (would exceed current 900s timeout) |
| Composite improvement vs Opus | — | +0.021 / +0.016 / +0.017 / **−0.007** |
| Per-dim drops >10% tolerance | 0 | **4** (3 on search-intent: readability_structural, entity_coverage, entity_graph; 1 on seo-for-roofing: entity_coverage) |

The plan's Sonnet-swap rationale was speed. Sonnet is slower, with uneven quality. Rejected. Opus stayed on propose and regen (until #43).

### Cache epoch rationale

`v3.6-highest-bar → v3.7-no-judge` because the composite's dim set AND denominator both changed. Every cached score becomes stale. The `judge` key in `score_cache.MODEL_VERSIONS` was also removed — keeping it would imply the judge still affects scoring, which it does not.

### Residual risk

- **Stale-HEAD worker writes**: async coordinators iterate independently. Worker A may be mid-regen when Worker B's proposal merges — A's proposal is then based on the old HEAD. The merge queue is last-writer-wins on SKILL.md; if A's edit conflicts, the merge overwrites. Consequences are bounded: A's rule change is lost, A's next iteration refreshes to current HEAD and re-proposes if the pattern still holds. No correctness concern, just wasted work.
- **Per-keyword regression cascade**: without the cross-keyword gate, it is possible for a sequence of keepers to each degrade another keyword by <1% (inside noise). Each affected keyword's next pre-score catches cumulative drift against its own baseline, so the corrective proposal lands within 1-2 iterations.
- **Dead-code surface**: `score_llm_judge`, `_gemini_generate_json`, `GEMINI_MODEL`, `score_geo_structural_readiness`, `score_geo_visibility_simulation` all remain in `evaluate.py` as dormant code. If anything outside scoring ever imports them, the import succeeds — no regression. Reinstating any of them is a `WEIGHTS` edit + `run(...)` line + epoch bump.

### Lesson

"Build an upfront validation gate to catch bad edits" is tempting but expensive and noise-prone. The cross-keyword gate cost 1290s per keep commit to reject edits the loop's next iteration would naturally catch within one additional cycle. The test for whether a gate is worth its cost: *would the loop's next iteration catch the same problem with no human involvement?* If yes, the gate is pessimism, not correctness.

Bench before assuming: a different model name is not automatically faster.

## 43. Generation backend moved to the Gemini SDK — two-phase plan

**Decision** (2026-04-16): `scripts/propose.py` and `scripts/regen.py` now call Gemini models directly via the `google-genai` SDK through a shared wrapper `scripts/gemini_backend.py`. Propose uses `gemini-3.1-pro-preview` with `thinking_level=HIGH`; regen uses `gemini-3-flash-preview` (no thinking override). Per-call ceilings started at 500 s and were raised to 1200 s (see Timeouts). The loop runs against an inferior generator **by design** as phase 1 of a two-phase plan.

### Two-phase rationale

Phase 1 uses Flash for regen because it's fast and cheap — iterating rule edits cheaply is the bottleneck when the proposer is ideating frequently. If rule changes can measurably lift dim scores against an inferior generator, they'll lift at least as far against a stronger one (the converse is not necessarily true — a rule that looks good on a weak generator may be over-fit to its failure modes). Phase 2 swaps regen to a stronger generator (Opus 4.6 or `gemini-3-pro-preview`) after the first convergence — only if `autoresearch-results.tsv` shows remaining headroom on any dim.

Propose stays on Gemini 3.1 Pro across both phases: the 2026-04-16 bench showed Pro produces real rule edits on 4/4 keywords with max 211s wall-clock (well under the 600s ceiling that propose.py carried for Opus). Reasoning quality on propose is what matters; execution quality on regen can be iterated through.

### What's in scope

| File | Change |
|---|---|
| `scripts/gemini_backend.py` | **New.** One module owns the `genai.Client`, the `call()` helper with `ThreadPoolExecutor` hard-timeout wrapper (the SDK's `http_options.timeout` is a known no-op — python-genai #911, #1893), `rate_gate_pro()` for 25 RPM defensive spacing, and the model-ID + timeout constants. Both propose + regen + the bench import from here so bench and prod can't drift. |
| `scripts/_regen_prompt.py` | Added `build_regen_prompt_for_gemini(slug, cwd)`: inlines SKILL.md and asks for the full markdown back. Sentinel and empty-response contracts preserved. |
| `scripts/_propose_prompt.py` | Added `build_propose_prompt_for_gemini(slug, score_report, cwd)` that delegates to `build_propose_prompt` for the score-table + per-dim semantics + git-log + results-tail context, then appends a Gemini output contract: return the FULL rewritten SKILL.md as response text. Caller writes that text to disk. |
| `scripts/regen.py` | Body rewritten. `regenerate_article(slug, cwd, timeout_s)` now calls `gemini_backend.call()` with the Gemini-shaped prompt, writes the response to `content/{slug}.md`, runs `_validate_output` on the file. `_validate_output` + `MIN_BODY_WORDS` + the `<!-- phase-4: pass -->` sentinel contract are unchanged — all existing tests in `tests/test_regen_validate.py` pass against the new body. `DEFAULT_TIMEOUT_S` now re-exports `gemini_backend.REGEN_TIMEOUT_S` (1200 s). |
| `scripts/propose.py` | Body rewritten. `propose(slug, worktree, weakest_dim_report, timeout_s)` now calls `gemini_backend.call()` with `thinking_level="HIGH"`, diffs the response against the on-disk SKILL.md via `difflib.unified_diff`, and writes the response to `worktree/rules/content-generation/SKILL.md` only if the diff is non-empty. No-edit outcome (identical response) returns False → iter_worker rc=12 (the no-edit path). Out-of-scope scope check preserved. `DEFAULT_TIMEOUT_S` now re-exports `gemini_backend.PROPOSE_TIMEOUT_S` (1200 s). |
| `scripts/bench_gemini_vs_opus.py` | Refactored to import from `gemini_backend` + the shared Gemini prompt builders instead of duplicating them. Removed ~140 lines of prior duplication. Model IDs + timeouts re-export from `gemini_backend` so future swaps are a one-line edit. |

### What stays unchanged

- `scripts/iter_worker.py` — unchanged. The public signatures of `propose.propose()` and `regen.regenerate_article()` are preserved; iter_worker doesn't know or care which backend the functions call.
- `scripts/parallel_loop.py` — unchanged. Async coordinator architecture from #42 carries over directly.
- `scripts/eval_server.py`, `scripts/evaluate.py`, all scorer code — unchanged. Scoring doesn't call Gemini (decision #42 removed the judge dim); the only Gemini usage left in `evaluate.py` is dormant.
- `scripts/run_until_done.sh` — unchanged. Backend is inherited from the Python subprocess env.
- `autoresearch-state.json.baselines`, `weights_epoch=v3.7-no-judge`, the 10 SERP dims, `n/82` weights — all unchanged. The backend swap does not touch scoring semantics.
- Tests — 68 tests still pass. `tests/test_regen_validate.py` exercises `_validate_output` which is preserved; dry-run tests in `test_async_workers.py` and `test_iter_worker.py` skip both propose and regen entirely.

### Why not make this a config/env-var switch?

The plan is explicit: Gemini now, a stronger generator next. The switch will be manual when convergence hits. A feature-flag abstraction ahead of a one-time toggle is over-engineering (YAGNI). When phase 2 launches, the plan says to swap the backend by editing `gemini_backend.FLASH_MODEL`. No dispatch layer necessary.

### RPM budget analysis

Gemini tier used:
- `gemini-3-flash-preview`: 1000 RPM / 2M TPM
- `gemini-3.1-pro-preview`: 25 RPM / 2M TPM

Worst-case call rate with 4 async coordinators:
- Regen: 4 coordinators × (1 regen per ~2-4 min iter) ≈ 1-2 calls/min. RPM ceiling 1000. No pressure.
- Propose: 4 coordinators × (1 propose per ~2-4 min iter) ≈ 1-2 calls/min. RPM ceiling 25. Comfortable but defensively floored by `rate_gate_pro()`'s 3s file-based spacing gate (fcntl + shared state file at `/tmp/autoresearch-gemini-pro-last-call.lock`) so synchronized bursts across coordinators can't blow the ceiling.

TPM is not a practical concern — worst-case per-call tokens are ~80K (Flash with aggressive thinking) and ~30K (Pro with HIGH thinking).

### Timeouts — 5× observed max (revised for multi-day runs)

Bench data on 2026-04-16 (see `docs/benchmarks/gemini-vs-opus.md`):
- Flash regen: 44-242s across 4 keywords (min 44, median 142, max 242).
- Pro 3.1 propose: 112-211s across 2 keywords of a partial rerun.

Initial ceilings landed at 2× observed max (500s each). Raised to **1200s (20 min) for both** because the loop is designed to run unattended for multiple days. The per-call ceiling catches stuck calls; it's not a runtime cap. At 2× headroom we were dropping legitimate-but-slow calls (thinking-budget spikes, Google-side latency hiccups) the loop would otherwise have kept. 5× headroom trades one bad minute of wall-clock on a real hang for strictly better utilisation on the 90%+ case.

Hard-enforced by `concurrent.futures.ThreadPoolExecutor(max_workers=1)` + `future.result(timeout=...)` in `gemini_backend.call()`. On timeout, the executor is shut down with `wait=False, cancel_futures=True`; the underlying thread may linger until the OS drops the TCP connection, but doesn't block the main flow.

`run_until_done.sh` `--max-restarts` also raised 50 → 500 to accommodate multi-day operation. Backoff is `30 * attempt` capped at 300s, so worst-case total backoff if every restart instantly fails is ~42 hours — a cliff that only matters if the infrastructure is fundamentally broken, in which case restarting forever is not the right response anyway.

### Residual risk

- **Quality drift under Flash regen.** Bench showed Flash produces short articles on "short-thinking" runs (44-46s wall-clock → content_length score 0.45-0.61). The loop may accumulate rule edits that compensate for Flash's output-length bimodality, and those edits may not be universal (phase 2 could reveal them as Flash-specific artifacts). Acceptable — the loop gates on per-keyword composite; if a Flash-specific rule hurts cross-keyword generality we'll see it in the stagnation pattern.
- **Gemini API outages / 429 cascades.** Unlike a local subprocess, Gemini calls depend on Google's API being up and within quota. A 429 storm propagates to every iteration; the outer `run_until_done.sh` retry loop catches most transient issues but a genuine multi-hour outage will exhaust `--max-restarts 500`.
- **Mid-iteration SKILL.md write races.** The new propose path writes to the worktree's SKILL.md synchronously before returning True. No new race surface beyond the single merge thread.

### Lesson

"Inferior model is bad" is the wrong frame when the inferior model is a deliberate phase-1 choice. The bench rejected Flash for production regen (7 per-dim breaches including -55% content_length). Shipping Flash anyway, as a controlled phase 1, is correct *if* the phase-2 plan is real. It would be wrong if there were no plan to graduate; here, the plan is explicit. Record: the bench output is an input to the decision, not the decision itself.
