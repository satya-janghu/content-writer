# Benchmarks

Reports behind the major design decisions. All runs date from 2026-04-16.

- [`skill-architecture-ablation.md`](skill-architecture-ablation.md): multi-phase (15 files) vs single-pass vs compressed skill, plus the post-ablation fixes. The compressed skill became production (decision #41).
- [`sonnet-vs-opus.md`](sonnet-vs-opus.md): Sonnet 4.6 vs Opus 4.6 as the generator. Sonnet was 2.67x slower and was rejected (decision #42). Raw data: [`sonnet-vs-opus.json`](sonnet-vs-opus.json).
- [`gemini-vs-opus.md`](gemini-vs-opus.md): Gemini 3 Flash and Pro vs Opus 4.6. Led to the Gemini generation backend (decision #43).
- [`pipeline-profiling.md`](pipeline-profiling.md): one fully instrumented iteration under the batch architecture; its numbers drove the async redesign (decision #42).
