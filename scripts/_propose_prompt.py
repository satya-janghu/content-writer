"""
Prompt builder for the rule-proposer model (Gemini 3.1 Pro via propose.py).

Realigned with the autoresearch design (decision #38): the LLM does BOTH
ideation (which lever to pull) AND modification (the rule edit). Earlier
revisions pre-selected a "weakest dimension" in Python and handed the agent
a single target — that pushed the agent toward "no edit" whenever the
existing rules already mentioned the targeted dim's surface concept (see
decisions #36).

This builder gives the agent:
  - the FULL per-dim score breakdown (score, weight, contribution, headroom),
  - per-dim measurement details (what the scorer actually saw),
  - one-line scorer semantics per dim (what each measurement MEANS),
  - recent git log of past rule-edit experiments (memory of what was tried),
  - recent results.tsv tail (per-iteration outcomes),
  - the list of rule files in scope.

It then asks for ONE rule edit the agent predicts will lift the composite,
with explicit refusal of "no edit" when there's measurable headroom.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


RULE_DIR_REL = "rules/content-generation"
SKILL_FILE_REL = f"{RULE_DIR_REL}/SKILL.md"
OUT_OF_SCOPE_DIRS = (
    "data/",                  # harness inputs
    "content/",               # output; regen's job
    "scripts/",               # harness code
    "autoresearch-state.json",
    "autoresearch-results.tsv",
)


# One short line per dim. Explains WHAT the scorer measures and how the
# detail fields decode — enough for the agent to translate a numeric weakness
# into a rule-shaped intervention. Does NOT prescribe which rule file to edit
# (that's the agent's reasoning).
SCORER_SEMANTICS: dict[str, str] = {
    "topical_coverage":
        "BERTopic clusters extracted from competitors; BGE cosine of each section "
        "to the cluster centroid. `coverage` = breadth × depth (top-3 sections per "
        "cluster). Low = missing whole subtopics; mid = present but shallow.",
    "keyword_relevance":
        "bge-reranker-v2-m3 scores (keyword, paragraph) for every paragraph. "
        "`strong` ≥ 0.5, `moderate` ≥ 0.3. Score = strong-rate weighted by length. "
        "Low = paragraphs drift off-topic; mid = present but generic.",
    "corpus_match":
        "Three legs: BM25 (top competitor terms as query, content as doc), "
        "TF-IDF cosine, BGE semantic cosine. TF-IDF and BGE legs normalised "
        "against `max(competitor cross-similarity)` — ceiling = the most "
        "corpus-coherent competitor. Low = vocabulary diverges from competitors.",
    "keyword_profile":
        "KeyBERT extracts top n-grams from content; BGE cosine of best-match against "
        "each competitor's KeyBERT terms. Aggregated `0.6*mean + 0.4*min` so a "
        "single uncovered competitor drags the score. Low = competitor-specific "
        "framings/jargon are missing.",
    "entity_coverage":
        "GLiNER zero-shot NER on content + competitors. `canonical_overlap` = "
        "depth-graded coverage of entities that ≥2 competitors share — each "
        "canonical entity scores `min(mentions/3, 1.0)`, averaged. So "
        "canonical_hits/canonical_count tells you breadth, while "
        "canonical_overlap/canonical_hits tells you per-entity depth (low ratio "
        "= entities are present but only mentioned once or twice). `mean_overlap` "
        "= per-competitor entity-set overlap (breadth across each competitor).",
    "entity_graph":
        "NetworkX co-occurrence graph (entities co-occurring in the same paragraph). "
        "Score = 0.5*node_overlap + 0.3*weighted-edge-Jaccard + 0.2*density-similarity. "
        "Low node_overlap = missing entities; low Jaccard = entities don't co-occur "
        "the way they do in competitors (entities mentioned in isolation).",
    "narrative_coverage":
        "BART-large-CNN summary of content vs each competitor; BGE cosine. "
        "Aggregated `0.6*mean + 0.4*min`. Low = the article's overall narrative "
        "arc differs from competitors' (different framing, different through-line).",
    "readability_structural":
        "textstat band (Flesch-Reading-Ease + Gunning Fog + Dale-Chall) + heading-count "
        "win-rate vs competitors + answer-block-ratio win-rate. Low = either too "
        "complex, too few headings, or too few extractable answer blocks.",
    "content_length":
        "Hard floor = 70% of shortest competitor; soft max = 2× longest; hard cap "
        "7000. Score is 1.0 inside the band, decays at the edges.",
    "banned_words":
        "Penalty 0.03/hit from `rules/content-generation/banned-words.md` "
        "(scorer data, parsed by evaluate.py; NOT a proposer target). Score = "
        "max(0, 1.0 - 0.03*hits). Top hits listed in detail.",
}


def _git_history(cwd: Path, n: int = 30) -> str:
    """Recent commit messages from the worktree's HEAD. Used as autoresearch
    memory — the agent learns from kept rule edits and avoids repeating
    reverted experiments. Falls back to '(no git history)' on any error."""
    try:
        proc = subprocess.run(
            ["git", "log", "--oneline", f"-{n}"],
            cwd=str(cwd), capture_output=True, text=True, check=True, timeout=10,
        )
        return proc.stdout.strip() or "(no commits yet)"
    except Exception:
        return "(git history unavailable)"


def _results_tail(project_root: Path, n: int = 20) -> str:
    """Tail of autoresearch-results.tsv. Each row = one iteration with status
    (keep/discard/crash) and metric delta. Empty header-only file is normal
    on first launch."""
    tsv = project_root / "autoresearch-results.tsv"
    try:
        text = tsv.read_text(encoding="utf-8")
    except OSError:
        return "(results.tsv not found)"
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return "(empty)"
    # Keep all comments + header + last n data rows
    header_end = 0
    for i, ln in enumerate(lines):
        if not ln.startswith("#") and "\t" in ln:
            header_end = i
            break
    head = lines[: header_end + 1]
    data_tail = lines[header_end + 1 :][-n:]
    if not data_tail:
        return "\n".join(head + ["(no iterations logged yet)"])
    return "\n".join(head + data_tail)


def _format_dim_table(dims: dict, details: dict) -> str:
    """Markdown table: dim | score | weight | contrib | headroom | semantics."""
    rows = ["| dim | score | weight | contrib | headroom | what the scorer measures |",
            "|---|---|---|---|---|---|"]
    # Sort by descending headroom so the highest-leverage dims surface first,
    # but still show all dims so the agent can spot multi-lever opportunities.
    by_headroom = sorted(
        dims.items(),
        key=lambda kv: (-float(kv[1].get("headroom", 0.0)), kv[0]),
    )
    for name, d in by_headroom:
        score = float(d.get("score", 0.0))
        weight = float(d.get("weight", 0.0))
        contrib = float(d.get("contrib", 0.0))
        headroom = float(d.get("headroom", 0.0))
        sem = SCORER_SEMANTICS.get(name, "(no semantic registered)")
        rows.append(f"| {name} | {score:.4f} | {weight:.4f} | {contrib:.4f} | "
                    f"{headroom:.4f} | {sem} |")
    return "\n".join(rows)


def _format_details_block(details: dict) -> str:
    """One JSON block per dim with the scorer's measurement detail. Shows the
    raw numbers the agent needs to reason about which rule lever exists."""
    if not details:
        return "(no per-dim details available)"
    blocks = []
    for name in sorted(details.keys()):
        blob = details[name]
        try:
            blocks.append(f"### {name}\n```json\n{json.dumps(blob, indent=2, sort_keys=True)}\n```")
        except (TypeError, ValueError):
            blocks.append(f"### {name}\n`(unserialisable)`")
    return "\n\n".join(blocks)


def build_propose_prompt(
    slug: str,
    score_report: dict,
    cwd: Path,
    project_root: Path | None = None,
) -> str:
    """Render the shared proposer context (score table, scorer semantics, memory, scope).

    `score_report` is the dict from `evaluate._weakest_dim_report` — it carries
    `composite`, per-dim `dims` (score/weight/contrib/headroom), and the full
    measurement `details` for every dim. The agent reads the whole picture and
    decides where to intervene.

    `project_root` defaults to `cwd`; pass the parent repo path if you want
    git history + results.tsv read from outside the worktree (rare).
    """
    pr = Path(project_root) if project_root is not None else Path(cwd)
    rule_dir = (cwd / RULE_DIR_REL).as_posix()
    skill_file = (cwd / SKILL_FILE_REL).as_posix()

    composite = score_report.get("composite", "?")
    dims = score_report.get("dims", {})
    details = score_report.get("details") or {}
    # `_weakest_dim_report` does not currently propagate `details`; fall back
    # to the legacy `weakest_detail` if that's all we have.
    if not details and score_report.get("weakest_detail"):
        details = {score_report["weakest_dim"]: score_report["weakest_detail"]}

    dim_table = _format_dim_table(dims, details)
    details_block = _format_details_block(details)
    git_log = _git_history(pr, n=30)
    results_tail = _results_tail(pr, n=20)

    # Total realisable composite headroom — useful framing so the agent
    # understands the size of the pie they're playing for.
    total_headroom = sum(float(d.get("headroom", 0.0)) for d in dims.values())

    return f"""\
You are one iteration of an autoresearch loop. The skill at
`{skill_file}` is a single-file content-generation instruction set that
writes SEO articles. Your job: make ONE edit to that file that you predict
will improve the composite score on the `{slug}` article. The harness will
regenerate content under your edited skill, score it, and keep the edit
only if the composite improves on every keyword (not just `{slug}`).
Otherwise it reverts.

Composite now: **{composite:.4f}**. Total movable headroom across the 10
dims: **{total_headroom:.4f}**. The dim table below is sorted by headroom
descending — the top rows are where rule changes have the most leverage.

## Per-dimension score breakdown

{dim_table}

`headroom = (1 − score) × weight` is the maximum composite gain if that dim
went to 1.0. `contrib = score × weight` is what that dim is buying you now.
Dims at score = 1.0 are ceiling-locked.

## Per-dimension scorer details

{details_block}

## Memory — recent git history (this worktree)

```
{git_log}
```

## Memory — recent iterations (autoresearch-results.tsv tail)

```
{results_tail}
```

## Scope

- Mutable: exactly one file — `{skill_file}`. The whole skill lives in that
  one SKILL.md; editing it is how the loop optimises the generation rules.
- Out of scope: everything else. Specifically: `content/`, `data/`,
  `scripts/`, and any file in
  `{RULE_DIR_REL}/` OTHER THAN `SKILL.md` —
  `{RULE_DIR_REL}/banned-words.md` is scorer data, parsed by
  evaluate.py; leave it alone. Touching any out-of-scope file rejects
  the proposal.
- The skill must remain universal — no keyword-specific rules.
- Do not create new files. Edit `SKILL.md` in place.
- Do not commit or run git. The orchestrator handles VCS.

## How to act

Don't describe a change you "would" make — make it. The only artifact
that matters is a real diff on `{SKILL_FILE_REL}`.

Read `SKILL.md`. Cross-reference against the score table. Pick the lever
you predict is most likely to lift the composite. Edit. Stop.

## On "no edit"

If you're tempted to claim the skill already covers the weakness: a rule
that mentions the surface concept is not the same as a rule the generation
pipeline obeys. The composite reflects what the pipeline actually produced,
so if a dim is weak the rule is either (a) missing, (b) too vague to be
followed, or (c) being overridden by a stronger conflicting rule elsewhere.
Tightening with numeric thresholds, concrete examples, or explicit
anti-patterns is a real edit.

If you exit without editing `{SKILL_FILE_REL}`, the iteration is wasted.
"""


def build_propose_prompt_for_gemini(
    slug: str,
    score_report: dict,
    cwd: Path,
    project_root: Path | None = None,
) -> str:
    """Gemini-shaped propose prompt.

    Reuses build_propose_prompt to assemble the exact same score-table +
    per-dim semantics + git-log memory + results-tail context, then appends a Gemini-shaped output contract: inline the
    current SKILL.md and ask for the FULL rewritten SKILL.md back (Gemini
    returns text and cannot edit files). The caller is responsible for
    writing the response back to disk.
    """
    base = build_propose_prompt(slug, score_report, cwd, project_root)
    skill_path = cwd / SKILL_FILE_REL
    skill = skill_path.read_text(encoding="utf-8")
    return (
        f"{base}\n"
        f"\n"
        f"---\n"
        f"\n"
        f"## Current SKILL.md (inlined — you have no file tools)\n"
        f"\n"
        f"```markdown\n"
        f"{skill}\n"
        f"```\n"
        f"\n"
        f"## Output contract\n"
        f"\n"
        f"- Return the FULL rewritten SKILL.md as your response.\n"
        f"- Preserve the YAML frontmatter block (lines between the two `---`\n"
        f"  markers) byte-for-byte.\n"
        f"- Make exactly one focused rule edit. Do not restructure the whole\n"
        f"  skill. The change should be diff-localised to a single section.\n"
        f"- Do not wrap the response in a markdown code fence.\n"
        f"- Do not prepend explanation, narration, or commentary.\n"
        f"- If you genuinely see no leverage worth an edit, return the SKILL.md\n"
        f"  unchanged. The diff detector will flag a no-op and the iteration\n"
        f"  will be dropped as 'no edit'.\n"
    )
