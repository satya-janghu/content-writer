# Contributing

Ground rules for changing this repository. The project tunes a universal content-generation skill against a fixed benchmark, so what may change depends on whether a loop run is in progress.

## Two modes

**Harness-dev mode**: setting up, porting, hardening, or improving the evaluation harness itself. The autoresearch loop is not running. Everything except secrets can change, including `scripts/*.py`, `data/**` and the skill, provided you re-baseline after any scoring-affecting change (see "Scoring-semantic changes").

**Loop mode**: the autoresearch loop is actively running. The table below is strict.

| Layer | Path | Mutable in loop mode? |
|-------|------|------------------------|
| Evaluation harness | `scripts/*.py`, `data/**` | No. These are the benchmark. |
| Content files | `content/*.md` | Only via regeneration, never direct edits |
| Skill definition (entire ruleset) | `rules/content-generation/SKILL.md` | Yes. This is the single file the loop optimizes. |
| Secrets | `.env` | `.env` (gitignored; holds GEMINI_API_KEY). Never committed in either mode. |

In loop mode, direct content edits produce keyword-specific artifacts that do not generalize, and harness edits break the benchmark.

**Crossing from harness-dev to loop mode** requires: (1) `python scripts/evaluate.py content/ --config data/keywords.json --verbose` runs clean end to end, (2) the resulting scores are recorded as the new baselines via `--establish-baselines`, and (3) those baselines are committed. Once crossed, treat the harness as frozen until a deliberate exit back to harness-dev mode (for example a scoring-semantic change).

## Baselines are not hardcoded

Baselines live in `autoresearch-state.json.baselines` and are established by `python scripts/parallel_loop.py --iterations 1 --establish-baselines` after any scoring-semantic change (judge swap, weight change, cache-epoch bump). `load_baselines()` reads from state.json, and the production loop refuses to start if the block is empty.

## Scoring-semantic changes

Anything that changes what a score means (a new or removed dimension, a weight, a normalisation, a model swap) requires three steps:

1. Bump `score_cache.MODEL_VERSIONS["weights_epoch"]`.
2. Re-establish baselines.
3. Log a numbered entry in `docs/decisions.md`.

## Skill edits

- Edit `rules/content-generation/SKILL.md` in place.
- Use universal rules only. Keyword-specific rules defeat the purpose of the benchmark.
- Do not add supplementary rule files; the single-file design is deliberate (lesson from decision #41).

## `banned-words.md` is scorer data

`rules/content-generation/banned-words.md` is parsed by the scorer (dimension 10). Its section headers are pinned by `tests/test_banned_words_parser.py`; renaming one changes scoring.

## Before a long run

`pytest` and `python scripts/doctor.py` must both be green. See `docs/RUNBOOK.md` for the full operator sequence.

## Tests

The suite has 68 tests. The CPU-safe subset runs without a GPU because the ML calls are monkeypatched. Outside a git checkout, tests that need a repository skip.

```bash
pip install -r requirements.txt
pytest tests/
```
