"""
Single source of the content-regeneration prompt.

regen.py (the loop) and bench_gemini_vs_opus.py (the benchmark) both build
the generator prompt here, so a tweak ships to every call site at once.

The skill is a single-pass instruction set (decisions #41) stored at
`rules/content-generation/SKILL.md`. The prompt inlines that file, so the
generator needs no file access: it drafts the article and returns one
markdown document ending with the completion sentinel.
"""
from __future__ import annotations

from pathlib import Path


# Keyword → natural-language one-sentence prompt. These are the same strings
# used to produce the v3.1 baselines and every baseline since.
# Do not drift the phrasing — the skill infers register, slug, and content type
# from these exact sentences.
KEYWORD_PROMPTS: dict[str, str] = {
    "seo-for-roofing": "Write a guide on SEO for roofing companies",
    "seo-for-saas":    "Write a guide on SEO for SaaS companies",
    "aeo-vs-seo":      "Write an article about AEO vs SEO",
    "search-intent":   "Create an article explaining what search intent is",
}


def build_regen_prompt_for_gemini(slug: str, cwd: Path) -> str:
    """Gemini-shaped regen prompt.

    generate_content is text-in / text-out, so SKILL.md is inlined into the
    prompt and the model returns the full markdown as its response. Preserves
    the <!-- phase-4: pass --> completion sentinel and empty-response abort
    contracts so regen._validate_output gates the output.
    """
    prompt_sentence = KEYWORD_PROMPTS.get(slug)
    if not prompt_sentence:
        raise KeyError(f"no keyword prompt defined for slug {slug!r}. "
                       f"Known: {sorted(KEYWORD_PROMPTS.keys())}")
    skill_path = cwd / "rules" / "content-generation" / "SKILL.md"
    skill_text = skill_path.read_text(encoding="utf-8")

    return (
        f"You are executing the content-generation skill below as a single-pass "
        f"universal article generator. Apply every rule in the skill to the "
        f"one-sentence prompt at the bottom and return the full article markdown.\n"
        f"\n"
        f"=== BEGIN SKILL.md ===\n"
        f"{skill_text}\n"
        f"=== END SKILL.md ===\n"
        f"\n"
        f"Output contract:\n"
        f"- Return ONE complete markdown document: YAML frontmatter (per the "
        f"  template in the skill) followed by the article body.\n"
        f"- End the document with the literal sentinel line "
        f"  `<!-- phase-4: pass -->` on its own line, preceded by a newline.\n"
        f"- On a hard-refusal (per the skill's hard-refusals section), return "
        f"  the empty string. An empty response is the abort signal. Do NOT "
        f"  silently repair around a hard refusal.\n"
        f"- Do not prepend explanation, narration, or commentary. Your entire "
        f"  response is the markdown document.\n"
        f"\n"
        f"Invoke the skill with this one-sentence prompt:\n"
        f"\n"
        f"  {prompt_sentence}\n"
    )
