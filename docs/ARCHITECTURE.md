# Architecture

*An autonomous framework for optimising a universal SEO / AEO / GEO content-generation skill against competitive benchmarks, with per-keyword independent gating.*

**Version**: v3.7 (cache epoch `v3.7-no-judge`)

---

## 1. Abstract

This document describes an end-to-end framework for iteratively improving a general-purpose SEO content-generation skill so that the content it produces ranks competitively against real-SERP benchmarks *and* is shaped for citation by generative engines (ChatGPT, Perplexity, Google AI Overviews, Gemini). The central artefact optimised by the loop is a single consolidated rule file, `rules/content-generation/SKILL.md` (see decisions #40, #41), that a language model reads during content generation. The loop proposes single-file edits, regenerates content under the new rule set, scores it across 10 weighted dimensions, and keeps the edit only if the keyword's post-score composite beats its baseline (a worker-local gate). Scoring runs on a long-lived GPU daemon with a per-article cache. Parallelism across the four keywords is achieved via git worktrees and a serial merge queue that preserves a linear rule history on `master`. The system is designed to run autonomously for days without human oversight, with an outer restart wrapper, a structured checkpoint/resume protocol, a liveness watchdog, and UTC-timestamped structured logging at every timing-critical boundary (`scripts/slog.py`).

The scoring composite is 10 SERP-competitive dimensions (normalised against competitor content), with weights stored as `n / 82` fractions summing exactly to 1.0 (table in section 4). The skill applies GEO (generative-engine-optimisation) rules inline within the consolidated `SKILL.md` (statistics density, claim, evidence, implication pattern, structural-element-per-H2, bold budget 5-10%), so content is shaped for AI citation at write time. The loop does not gate on GEO compliance because there is no SERP-ranked reference content that validates the specific absolute targets as rank-causal, and the one earlier attempt to add GEO scoring dimensions (v3.3-v3.4; removed in v3.5 per decisions #37) surfaced the compliance-gap pattern documented in #36. Empirical context: Chen et al. (2025) found near-zero correlation (Spearman rho = -0.02 to 0.11, n = 19,556 queries) between Google SERP rank and AI-citation rate, which is why keeping GEO as a generation concern rather than a scoring concern is defensible.

---

## 2. Problem statement

Given:

- A **content-generation skill** S consisting of natural-language rules in a single consolidated `SKILL.md` that a language model reads and executes in a single pass. The file covers SEO structure, AEO direct-answer patterns, EEAT signals, GEO citation rules, humanize prose and formatting, structural ranges, and banned-word guardrails, all in one document with no multi-phase pipeline and no cross-file handoffs. The 4-phase pipeline was collapsed into single-pass in decision #41 after a benchmark showed the consolidated version matched or beat multi-phase quality at 15% of the size and 3x the regen speed (see `docs/benchmarks/skill-architecture-ablation.md`).
- A set of K test keywords (currently 4: `seo-for-roofing`, `seo-for-saas`, `aeo-vs-seo`, `search-intent`) and for each keyword a **competitive profile**: embeddings, topic clusters, BM25 corpus terms, entity graphs, and summarised competitor articles from real SERPs.
- A **10-dimension composite score** S(c, p) in [0, 1] that maps (content c, profile p) to a scalar approximation of competitive quality. All 10 measure SERP-competitive parity (competitor-normalised or absolute policy floors).

Goal: modify S so that for every keyword k, the composite score of the article generated from S improves and does not regress below a committed floor. The target is a *universal* skill (rules that generalise across topics), not four keyword-specialised skills. A rule that improves `seo-for-roofing` at the expense of `search-intent` is worthless and is rejected by the per-keyword baselines.

Non-goals: no model training or fine-tuning, and no competitor examples in the generation prompt (the skill never reads external content at runtime). The work is curating a markdown rule set through discrete keep/revert decisions, in the spirit of autoresearch-style bounded iteration.

---

## 3. Methodology

### 3.1 The iteration body (one proposal)

Implemented in `scripts/iter_worker.py`; one worker process per keyword iteration.

```
  (a) PRE-SCORE     score the current article for keyword k via the daemon (cached)
  (b) PROPOSE       propose.py sends the full per-dim report plus git log and
                    results-TSV tail to gemini-3.1-pro-preview
                    (thinking_level=HIGH); it returns a rewritten SKILL.md.
                    Empty diff => outcome "no edit", worker returns rc=12.
  (c) REGENERATE    regen.py sends gemini-3-flash-preview a prompt with SKILL.md
                    inlined. Output must have frontmatter, at least 500 words,
                    and the <!-- phase-4: pass --> sentinel in its last 3 lines.
  (d) POST-SCORE    score the new article
  (e) LOCAL GATE    post_composite > baseline for that keyword
  (f) PROPOSAL      the proposal JSON goes to the MergeQueue, which applies
                    it to master and commits
```

Everything in (a)-(e) runs in a git worktree at `.autoresearch/worktrees/<slug>`, disjoint from `master`. Nothing is committed to `master` until the merge queue applies the proposal.

### 3.2 Per-keyword independence

The gate is per keyword: a proposal is a keeper only if `post_composite > baseline` for the keyword that proposed it (a worker-local gate). Earlier versions (v3.5-v3.6) also ran a cross-keyword check on every keeper: regenerate all four articles under the new rules, score all four, and commit only if none regressed. Decision #42 removed that regen-all and score-all gate. It cost 1,290 s per keep and rejected edits that fell inside the scoring noise band.

If one keyword's edit drifts another keyword, that keyword's next pre-score catches the drift: its `post_composite` is measured against the now-updated `master` HEAD, and its own proposer edits in the corrective direction. The loop self-corrects through iteration rather than upfront validation. There is no cross-keyword averaging, ranking, or "overall" score; each keyword is its own axis.

### 3.3 Parallelism

The loop runs 4 `worker_coordinator` threads (one per keyword) plus 1 `MergeQueue` drain thread. Each coordinator loops: refresh its worktree, spawn an `iter_worker.py` subprocess, wait cooperatively on `stop_event`, read the proposal, submit to the queue if it is a keeper, update its stagnation streak. The merge thread applies and commits keepers serially, so `master`, `SKILL.md` and the state file have a single writer and the rule history stays linear.

Worker processes run in their own process group (`start_new_session=True`), and `killpg(SIGKILL)` runs on stop so that no child outlives the supervisor. A coordinator gives up after `MAX_REFRESH_FAILS = 5` consecutive worktree-refresh failures (logged as `COORDINATOR_REFRESH_GIVE_UP`).

### 3.4 Convergence

Each keyword keeps a stagnation streak: consecutive iterations without a keep. The run converges when every keyword has a streak of at least `--max-stagnation` (default 10). `--iterations` sets a per-keyword ceiling (the wrapper defaults it to 10000, effectively unlimited).

### 3.5 Checkpoint and resume

After each commit, `autoresearch-state.json.loop` is written under an `fcntl` lock with `stagnation_streaks`, `last_commit_sha` and `completed_iters`. `--resume` reloads the streaks. There are no batches to replay: each coordinator's next worktree refresh prunes and recreates its worktree from the current `master` HEAD, so no mid-iteration state matters.

`scripts/run_until_done.sh` restarts the driver on any non-fatal exit with a backoff of 30 s x attempt (capped at 300 s); `--max-restarts` defaults to 500. Exit codes: 0 converged, 2 fatal config, 5 daemon would not start, 130 SIGINT.

---

## 4. Models and scoring

### 4.1 Scoring dimensions

The composite is 10 SERP-competitive dimensions with weights stored as `n / 82` fractions, so the rescale from the previous 11-dim composite (decisions #42) keeps ratios exact. Current cache epoch: `v3.7-no-judge`.

| # | Dimension | Weight | Method |
|---|-----------|--------|--------|
| 1 | Topical coverage | 25/82 ≈ 0.3049 | BERTopic clusters + BGE semantic similarity to cluster centroids, with depth factor (top-3 matching sections) |
| 2 | Keyword relevance | 10/82 ≈ 0.1220 | bge-reranker-v2-m3 scores (keyword, each paragraph). Strong ≥ 0.5, moderate ≥ 0.3. |
| 3 | Corpus match | 11/82 ≈ 0.1341 | BM25Okapi (top competitor terms as query) + TF-IDF cosine + BGE semantic cosine. All three legs normalised against `max(competitor cross-signal)` (decisions #35 P4 + #39 highest-bar) — ceiling = the most corpus-coherent competitor on each modality |
| 4 | Keyword profile | 5/82 ≈ 0.0610 | KeyBERT terms (content) vs each competitor, BGE semantic cosine of best match; per-competitor scores aggregated as `0.6 * mean + 0.4 * min` (decisions #35 P1) to penalise concentrated weakness |
| 5 | Entity coverage | 7/82 ≈ 0.0854 | GLiNER zero-shot NER with custom types; canonical = union of all entities mentioned by ANY competitor (decisions #39 highest-bar), depth-graded as `mean(min(1, count/3))` per entity (decisions #35 P2) + max per-competitor overlap + win-rate |
| 6 | Entity graph | 3/82 ≈ 0.0366 | NetworkX co-occurrence graph; linear node overlap (no saturation cap — decisions #35 P3) + weighted edge Jaccard + density similarity |
| 7 | Narrative coverage | 3/82 ≈ 0.0366 | BART-large-CNN summaries of content and competitors; BGE cosine similarity, aggregated as `0.6 * mean + 0.4 * min` (decisions #35 P1) |
| 8 | Readability + structural | 8/82 ≈ 0.0976 | textstat multi-metric (FRE + Gunning Fog + Dale-Chall) band-score + structural sub-scores `content / max(competitor)` (decisions #39 highest-bar — full credit only at the top competitor's heading count and answer-block ratio) |
| 9 | Content length | 5/82 ≈ 0.0610 | Policy: hard floor (70% of shortest competitor), soft max (2× longest), hard cap 7000 |
| 10 | Banned words | 5/82 ≈ 0.0610 | Penalty 0.03/hit from the authoritative list in `rules/content-generation/banned-words.md` |

`score_llm_judge`, `score_geo_structural_readiness`, and `score_geo_visibility_simulation` remain in `evaluate.py` but are not called by `evaluate_content`. They are retained so a future decision can re-enable any of them with a small diff. The `geo_sim_queries` field stays in the profile JSONs (inert). See decisions #42 for the removal of the Gemini judge from the scoring loop.

**Models:** BAAI/bge-large-en-v1.5 (1024-dim embeddings), BAAI/bge-reranker-v2-m3 (cross-encoder), urchade/gliner_large-v2.1 (zero-shot NER), BERTopic (topic modeling), facebook/bart-large-cnn (summarization), KeyBERT (keyword extraction), textstat (readability), rank-bm25 (lexical), NetworkX (entity graphs). All local. Gemini is no longer called during scoring (decisions #42); the client and key still load dormantly so a future reinstatement is a one-line diff.

### 4.2 Model inventory

All local models run on a single RTX 2070 Super (8 GB VRAM) via a long-lived Python daemon (about 5.5 GB VRAM after warmup). Gemini is a remote API call used only by the proposer and generator.

| Model | Purpose | Size / mode |
|---|---|---|
| `BAAI/bge-large-en-v1.5` | Universal embedding for topical/corpus/narrative similarity | 1024-dim, fp16 |
| `BAAI/bge-reranker-v2-m3` | Cross-encoder for keyword-paragraph relevance | fp16 |
| `urchade/gliner_large-v2.1` | Zero-shot NER with 14 custom entity types | fp16, `quantize` + `compile` when available |
| `KeyBERT` (+ BGE) | Bounded n-gram (1-3) keyword extraction with `max_features=2000` | CPU vectoriser |
| `facebook/bart-large-cnn` | Abstractive summaries for narrative-coverage dim | fp16, `tokenizer.model_max_length=1024` (critical fix; see decisions #27) |
| `BERTopic` (+ UMAP + HDBSCAN) | Topic modelling (CPU UMAP is fast enough at these document counts) | — |
| `gemini-3.1-pro-preview` | Rule-edit proposer (`propose.py`) | API, `thinking_level=HIGH`, temperature 0.0, 1200 s hard timeout, 3.0 s cross-process spacing gate for the 25 RPM tier |
| `gemini-3-flash-preview` | Content generator (`regen.py`) | API, temperature 0.0, 1200 s hard timeout |

**Division of labour**:

- **Gemini 3.1 Pro** does the reasoning task: given the full per-dimension score report, it decides which rule in `SKILL.md` to change and rewrites the file.
- **Gemini 3 Flash** runs the single-pass skill to produce a new article (one `SKILL.md` inlined in the prompt, one markdown document back ending with the `<!-- phase-4: pass -->` completion sentinel).
- Scoring never asks a model "is this article good?". Every scorer is a pure function of (content, profile), bit-for-bit deterministic given the same model weights.

### 4.3 Why each model

- **BGE (`bge-large-en-v1.5`)**: a top-ranked open embedding on MTEB at the time of setup, fits in VRAM alongside everything else, fp16-stable.
- **BGE-reranker-v2-m3**: paired natively with the base embedder; cross-encoder scoring is stricter than pure embedding similarity and worth the extra compute for the keyword-relevance dim.
- **GLiNER v2.1**: zero-shot with custom entity types. SEO-flavoured entities (tools, platforms, metrics, companies) are not known to a generic NER model. GLiNER's 384-token limit is respected by a sentence-aware chunker that subtracts the entity-prompt prefix length.
- **BART-large-cnn**: standard abstractive summariser; the narrative-coverage dim compares the article's summary to each competitor's summary. BART's 1024-position embedding is a hard constraint and the source of decision #27 (the cached `tokenizer.model_max_length` sentinel silently disables `truncation=True`).
- Opus 4.6 and Sonnet 4.6 were benchmarked as generators; see `docs/benchmarks/`.

### 4.4 Score cache and its invalidation

Every scored (content, profile) pair is cached in `autoresearch-state.json.article_scores[slug]`, keyed on `{sha256(content), mtime(profile), MODEL_VERSIONS}`. `MODEL_VERSIONS` is a pinned dict including each model identifier plus a `weights_epoch` string (currently `v3.7-no-judge`). A change in any component of the key invalidates that entry. Invalidation is by construction: entries are never explicitly expired, the key just changes.

The cache lives behind an `fcntl.flock(LOCK_EX)` so concurrent writers (the iter_workers each doing pre-score and post-score) cannot corrupt the JSON via a race on `os.replace`. Reads are lockless; a torn read surfaces as a cache miss, which is indistinguishable from a legitimate miss and causes a fresh score.

---

## 5. System architecture

### 5.1 Components

```
┌──────────────────────────────────────────────────────────────────┐
│  Host (Linux + NVIDIA GPU; reference: RTX 2070 Super, 8 GB)      │
│                                                                  │
│  ┌──────────────────┐                                            │
│  │ eval_server.py   │   long-lived; owns CUDA; listens on        │
│  │  - BART, BGE,…   │   /tmp/content-writer-eval.sock (UNIX)     │
│  │  - score_cache   │                                            │
│  └──────────▲───────┘                                            │
│             │ JSON-RPC (newline-delimited)                       │
│  ┌──────────┴───────┐     ┌────────────────┐                     │
│  │ 4 × iter_worker  │────▶│ .autoresearch/ │   proposals/*.json  │
│  │  (1 per keyword, │     │  proposals/    │                     │
│  │   in git worktree)     └────────┬───────┘                     │
│  └────────┬─────────┘              │                             │
│           │                        ▼                             │
│           ▼          ┌──────────────────────────────────────┐    │
│  ┌──────────────────┐│ parallel_loop.py      (the driver)   │    │
│  │ propose.py /     ││  - creates worktrees, spawns workers,│    │
│  │ regen.py         ││    drains merge queue, commits       │    │
│  │   → Gemini API   ││    keepers; updates state.json.loop  │    │
│  └──────────────────┘└──────────────────────────────────────┘    │
│             ▲                                                    │
│             │ restarts on non-fatal exit                         │
│  ┌──────────┴───────┐                                            │
│  │ run_until_done.sh│  outer autonomous wrapper                  │
│  │  - (re)spawns daemon                                          │
│  │  - backoff, max_restarts                                      │
│  │  - structured JSONL log /tmp/autoresearch-run.log             │
│  └──────────────────┘                                            │
└──────────────────────────────────────────────────────────────────┘
```

### 5.2 Directory layout

```
content-writer/
├── README.md
├── LICENSE
├── requirements.txt
├── .env.example                    GEMINI_API_KEY placeholder
│
├── docs/
│   ├── ARCHITECTURE.md             THIS FILE
│   ├── RUNBOOK.md                  operational guide
│   ├── CONTRIBUTING.md             mutability rules and contributor workflow
│   ├── decisions.md                numbered design decisions with rationale
│   └── benchmarks/                 model and skill-architecture benchmark reports
│
├── rules/content-generation/
│   ├── SKILL.md                    the autoresearch target (single consolidated file)
│   └── banned-words.md             scorer-parseable banned-word list (evaluate.py)
│
├── content/                        {slug}.md sample articles (disposable test output)
├── data/
│   ├── keywords.json               keyword → (slug, competitor URLs, content_file)
│   ├── README.md
│   ├── competitors/                scraped SERP competitors (gitignored)
│   └── profiles/                   BERTopic + BM25 + entity graph + BGE vectors (gitignored)
│
├── scripts/
│   ├── evaluate.py                 10-dim scoring; --suggest-weakest-dim, --verify-cache
│   ├── analyze_competitors.py      builds profiles from data/competitors/
│   ├── scrape_competitors.py       fetches the competitor articles listed in keywords.json
│   ├── eval_server.py              GPU scoring daemon
│   ├── eval_client.py              UNIX-socket client with in-process fallback
│   ├── score_cache.py              flock-protected cache + public mutate_state / read_state
│   ├── profile_freshness.py        hash-skip for analyze_competitors
│   ├── parallel_loop.py            driver: coordinators + merge queue + checkpoint
│   ├── iter_worker.py              single-keyword iteration body
│   ├── propose.py, _propose_prompt.py      Gemini rule-edit proposer
│   ├── regen.py, _regen_prompt.py          Gemini content regenerator
│   ├── gemini_backend.py           shared Gemini wrapper: timeouts, rate gate, retries
│   ├── bench_gemini_vs_opus.py     generator benchmark driver
│   ├── run_until_done.sh           autonomous wrapper (exit-code restart contract)
│   ├── install_deps.sh             venv + requirements + crawl4ai setup + doctor
│   ├── doctor.py                   blank-state health report
│   ├── watchdog.py                 liveness probe → .autoresearch/watchdog-alert.json
│   ├── bg_commit.py                detached git commit + flock-safe TSV append
│   └── slog.py                     structured UTC-timestamped logging
│
└── tests/                          pytest unit + integration suite (68 tests)
```

Runtime state (`autoresearch-state.json`, `autoresearch-results.tsv`, `.autoresearch/`) is created by the loop and is gitignored.

### 5.3 Daemon protocol

`eval_server.py` speaks newline-delimited JSON over a UNIX domain socket (mode 0600, at `/tmp/content-writer-eval.sock`, overridable with `EVAL_SERVER_SOCKET`). Ops:

- `health` → `{pid, models_loaded: [...]}`
- `score_article` → full 10-dim composite with optional cache hit/miss annotation
- `score_geo_structural` → GEO structural readiness, unused by the composite since v3.5 (retained for standalone rule-tuning and diagnostics)
- `flush_cache` → per-slug or global
- `shutdown` → graceful exit

Single-threaded. GPU models do not parallelise cleanly, so parallelism lives at the worker layer. Client-side transport errors fall back to in-process model loading (slower but correct), which is also the code path exercised by standalone `python scripts/evaluate.py` calls.

---

## 6. Design rationale (per-decision)

### 6.1 Why a custom loop rather than a scalar-metric optimiser

Generic goal-directed iteration tools handle a scalar metric: modify, verify, keep or discard, with optional bounded iteration and global plateau detection. This use case has two hard requirements they cannot express:

1. **Vector metric with per-axis gating**: a proposal is judged against the baseline of the keyword it targets, and the four keywords are independent axes. A scalar guard threshold has no "this axis, not the average" semantics.
2. **Rich per-dimension feedback**: the proposer needs the full 10-dimension report, scorer semantics, git history and results tail in its prompt, not a single number.

A custom loop also provides, for free, features generic tools lack: 4-way parallel fan-out, merge-queue rule-change serialisation, per-keyword convergence streaks, process-level checkpoint/resume, a score cache, a persistent daemon and a watchdog.

### 6.2 Why per-keyword independence?

The four test keywords deliberately cover disjoint topics (roofing services, SaaS marketing, AEO theory, search-intent theory) so that a "universal" skill must generalise. Cross-keyword averaging would mask regressions: a rule that adds 0.05 to roofing and subtracts 0.02 from search-intent looks positive on the mean but is a quality loss for the search-intent audience. Per-keyword independence is the definition of universality for this project.

### 6.3 Why a separate daemon?

Model-load cost on cold start is about 60 s (BART + BGE + GLiNER + reranker + KeyBERT). Every standalone `evaluate.py` invocation pays this cost, which adds up over a long run. A long-lived daemon that holds models resident and serves scoring requests over a UNIX socket amortises the cost to zero after the first start. The client falls back to in-process loading on transport failure, so standalone debugging still works.

### 6.4 Why parallel workers + serial merge queue (rather than fully parallel or fully serial)?

**Fully serial**: one keyword at a time. Simple, but 4x slower wall clock.

**Fully parallel (parallel commits)**: four workers commit rule changes to master concurrently. This races the shared `SKILL.md`; commit ordering is nondeterministic; the audit trail becomes unintelligible.

**Parallel propose + regen, serial commit (chosen)**: workers explore in isolation (worktrees), and only winners go through a serial queue. Rule history stays linear. Regeneration is the dominant cost, so parallelising it 4x captures most of the available speedup without the complexity of parallel commits.

### 6.5 Why `fcntl.flock` on the state file instead of moving persistence into the daemon?

Moving cache persistence inside the single-threaded daemon would eliminate races by construction. Pragmatic flock-around-rename gives the same guarantee on a single host with much less code: no new RPC ops, no client changes, no cross-process invariants to maintain. Daemon-single-writer is still the right move if the system is ever distributed across hosts, but that is not a near-term concern.

### 6.6 Why a 10-dim composite

A single LLM judge is noisy, biased toward surface features, and hard to audit. The 10-dim composite is now 100% local and deterministic given model weights: the Gemini judge that once carried 8/90 of the weight was removed in decision #42. Consequences:

- Iterations can move every dimension monotonically in response to rule changes, and the same inputs always produce the same score.
- A broken API can never fake-reject a good proposal, because scoring makes no network calls.
- It is possible to diagnose *which* dimension improved or regressed when a proposal lands. The `--suggest-weakest-dim` flag emits JSON that drives the diagnostic target; the reported `weakest_dim` is chosen by max composite headroom (`(1 - score) x weight`), not the numerically smallest score (decisions #34). For dims that emit `sub_scores` in the detail payload, the report additionally includes a `weakest_sub_score` pointer.

### 6.7 Why the 7000-word hard cap and the soft length range?

Empirical: competitive SERP articles in the benchmark range from about 1,200 to 3,500 words, and past 3,500 is generally padding. 7000 is 2x the upper soft bound; anything needing more belongs in a pillar structure (multi-document), not a single file. The cap doubles as a safety fuse: a runaway generator that produces a 50k-word article fails fast rather than wasting a scoring pass.

### 6.8 Why the specific convergence rule (10 consecutive non-keeps per keyword)?

The rule needs to tolerate noisy non-improvements (a proposal might fail through bad luck, not lack of headroom) while still stopping when further improvement is unlikely. 10 consecutive non-keeps per keyword is long enough to ride out random failures, short enough that a saturated keyword does not block progress on others indefinitely. It is a heuristic; a more principled version might use a statistical test on recent score deltas, but the extra machinery is not justified at current iteration volumes.

### 6.9 Why `run_until_done.sh` instead of making `parallel_loop.py` self-restart?

Separation of concerns. `parallel_loop.py` handles the *semantic* loop (workers, merge queue, gate); any crash in it should leave durable state and exit. `run_until_done.sh` handles the *operational* loop (daemon lifecycle, retry policy, backoff). A pure-Python self-restart would duplicate process-management logic that bash already does well, and would make the daemon's own restart harder to reason about.

### 6.10 Why every dim either scores or raises (no 0.5 fallback anywhere)

Gemini calls can stall at the transport layer for minutes, which would block the daemon and the loop. Two hardening steps are layered on top of the SDK.

**Hard per-attempt timeout (decision #30).** The google-genai SDK ignores user-supplied `http_options.timeout` because the client passes `timeout=None` to its internal httpx client, so a Python-level timeout is required. The approach: wrap every `generate_content` call in a fresh `concurrent.futures.ThreadPoolExecutor(max_workers=1)` and call `future.result(timeout=...)`. This is a Python-level timeout the SDK cannot bypass. It now guards the generation backend in `gemini_backend.call()` as well as the dormant judge. On timeout, `executor.shutdown(wait=False, cancel_futures=True)` returns immediately; the underlying thread keeps running until the OS eventually drops the TCP connection. Thread leakage is bounded: across an hours-long run the expected leak is single-digit threads in total.

**Outer retry + raise on exhaustion (decision #33).** Scoring never substitutes a neutral 0.5 fallback, since that would distort the composite and mislead the per-keyword gate (decision #33). Every Gemini-dependent dimension retries and raises on exhaustion. The dormant judge path (`_gemini_generate_json`) does 3 outer attempts with 3 / 9 / 27 s exponential backoff (the SDK's `HttpRetryOptions` still runs inside each attempt for 429/5xx), 404 short-circuits as terminal, and on exhaustion it raises `evaluate.ScoringError("gemini", reason)`. The scorer propagates the exception; `evaluate_content` propagates it; the daemon catches it at its generic handler and returns `{ok: False, error: str}`; `eval_client` raises `DaemonUnavailable`; `iter_worker` catches (both pre- and post-regen scoring sites) and returns rc=4, so the iteration is dropped.

The trade-off is deliberate: a persistent outage *halts progress on that keyword* rather than letting the loop run with degraded scores. Infrastructure errors are not quality signals, and a halted loop is easier to diagnose than a silently biased baseline. The same rule applies to every other scoring dimension (profile missing competitor data, BART OOM, KeyBERT error): raise, don't fabricate.

### 6.11 Why Gemini Pro proposes and Flash regenerates

Decision #43 moved both model calls to the Gemini SDK in a two-phase plan. In phase 1 the proposer is the reasoning model (`gemini-3.1-pro-preview`, HIGH thinking) and the generator is the fast model (`gemini-3-flash-preview`). The generator is deliberately the less capable one: if rule edits can lift dimension scores against a weaker generator, they will lift further against a stronger one, and iteration is cheap. In the benchmark, Flash averaged 0.51x the regen time of Opus 4.6 (142.6 s vs 282.2 s) at about the same average composite, and Pro produced real edits on 4 of 4 keywords. Phase 2 swaps the generator to a stronger model once phase 1 converges. See `docs/benchmarks/gemini-vs-opus.md`.

---

## 7. Desired outcome

A committed rule set in `rules/content-generation/SKILL.md` (the single-file skill) such that:

1. **Every keyword's composite score is at or above its committed baseline**: the per-keyword floor, established with `--establish-baselines` after any scoring-semantic change.
2. **At least one keyword's composite has measurably improved** over the run, and ideally all four have.
3. **The commit history tells a coherent story**: each kept commit has a descriptive message (`iter N keep (keyword, Δ=+0.xxx): gate reasons...`); rejected proposals are logged to TSV but not committed.
4. **No keyword-specific rules** in the final skill. Generalisation is the point. A spot-check of the final diff should reveal only rules that plausibly apply to arbitrary SEO content (all topics, all industries).
5. **Reproducible**: a fresh clone should pass `doctor.py`, and `--establish-baselines` should land at similar numbers (within ±0.01), with a re-run from HEAD continuing to pass the gate.

Beyond the rule set itself, the run produces:

- A TSV log of every iteration (keep / reject / dry-keep, with scores and delta).
- Git history where every commit is a principled rule improvement.
- Learnings captured in `docs/decisions.md` (new numbered entries for any discoveries).
- An estimate of convergence iteration count, informing whether the project benefits from *more* test keywords (to harden universality) or more iteration budget.

---

## 8. Design trade-offs and future work

### 8.1 Cache-key fragility

The cache key includes `profile_mtime` but not `profile_sha`. A `touch(1)` on a profile file with no content change invalidates the cache. This is paranoid but cheap (re-scoring costs about 20 s). Converting to a content hash is a one-line change if the thrash ever matters.

### 8.2 Merge-queue head-of-line blocking

The single merge thread is last-writer-wins on `SKILL.md`: keepers are applied serially in arrival order, and a later keeper's diff is applied on top of whatever the earlier keeper left. Two keepers proposed from the same base can therefore interact, and the second one's gain was measured without the first one's change. The per-keyword pre-score on the next iteration catches any resulting drift.

### 8.3 Convergence definition may be too loose

"Every keyword stagnates for 10 iterations" can be satisfied by a regime where *one* keyword keeps slow-improving for 100 iterations while the others plateaued. The loop keeps running while the other keywords hold steady. A tighter rule: stop when the *sum* of streaks exceeds K x max_stagnation. The looser rule was chosen so a single slow-converging keyword does not force abandonment; the operator can stop the run whenever it stops being worth the GPU time.

### 8.4 The skill is not self-describing

There is no runtime assertion that the skill's rule files match a schema. If a future edit introduces a typo in a section header that `load_banned_words` parses, scoring silently changes. `tests/test_banned_words_parser.py` is a regression net, but it only catches changes that have been considered. A schema-validation step on the skill directory would be a good belt-and-braces addition.

### 8.5 Research directions

1. **Is per-keyword gating too strict?** The alternative is a per-keyword margin (e.g. at least 0.95 x baseline), which would let marginal regressions through. Strict is plausibly correct for universality, and is revisited as more keywords are added.
2. **Are the 10 dimensions the right set?** Topical coverage at about 30.5% (25/82) still dominates. That weight was chosen because BERTopic clustering felt more robust than the other semantic signals; in practice it means a rule change that does not touch topical coverage can only move about 70% of the composite. Topical coverage saturates at 1.0 on all four keywords today; adding finer-grained topic-depth measurement is a planned refinement.
3. **Is the convergence rule principled?** 10 consecutive non-keeps is a pragmatic stopping threshold, configurable with `--max-stagnation`.
4. **Should the rule proposer see competitor content?** Currently it sees only the skill's own rule file and the per-dimension detail report. Showing it competitor summaries would probably help it propose more targeted edits but violates the self-contained-skill invariant that makes the skill portable.
5. **Single-host single-GPU: is horizontal scaling worth the complexity?** A second GPU could double throughput by overlapping merge-queue work with the workers. This has not been built.

---

## 9. Summary

The system iteratively improves a natural-language rule set that governs how a language model writes SEO content. The loop is autonomous: a reasoning model proposes a rewrite of the single skill file, a fast model regenerates the article under it, and a local 10-dimension scoring daemon evaluates the result against competitive profiles from the live top-ranking pages. A per-keyword gate keeps only measurable wins. Parallelism across four test keywords comes from git worktrees and a serial merge queue, and checkpoint/resume, daemon lifecycle management and a watchdog let the run continue for days without supervision. Baselines are established dynamically under the current cache epoch and live in `autoresearch-state.json.baselines`.
