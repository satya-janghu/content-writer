---
name: content-generation
description: Writes high-ranking SEO content from a simple one-sentence prompt. Use whenever the user says "write an article about X", "create a guide on X", "write a comparison of X vs Y", "write content about X", or asks for any SEO-optimized long-form written content (articles, guides, comparisons, how-tos, listicles, product reviews, pillar pages, FAQ pages). The skill produces publish-ready markdown with YAML frontmatter, applying SEO structure, AEO optimization, EEAT signals, and humanized prose — all without requiring competitor URLs or reference content.
---

# Content Generation

Draft a full long-form SEO article from a one-sentence prompt. Read this file once, compose in memory, and return the article as one Markdown document. No narration.

## Contract

Input: one sentence. Derive:
- **Keyword / topic** — the subject.
- **Slug** — lowercase-hyphens, ≤60 chars.
- **Content type** from shape: "What is X?"/"X explained" → blog; "X vs Y" → comparison; "How to X"/"SEO for X"/"Guide to X" → guide; "Top N"/"Best X" → listicle; "X review" → review; "Complete/ultimate guide to X" → pillar; stack of questions → FAQ.
- **Register** from topic: technical, operational, conceptual, or commercial. No personas ("as a senior X" is banned).
- **Schema** = `Article` + one of `HowTo` | `FAQPage` (only if ≥3 real Q&A pairs) | `Review` | `Product` | `BlogPosting` | `BreadcrumbList`.

Output: `content/{slug}.md` (or specified path). Complete YAML frontmatter + body. End file with the literal sentinel line `<!-- phase-4: pass -->`. An empty output is the abort signal on hard-refusal or word-cap breach — do not silently rewrite around a hard refusal.

No web access or external tools. Do not ask questions. Infer everything.

## Hard refusals (absolute — emit empty file if complying forces a violation)

1. Never claim overnight / guaranteed / timed rankings.
2. Never write "AI will replace SEO" takes in either direction.
3. Never fabricate case-study numbers, company names, or named-person quotes. If the exact number isn't known, frame with "studies show", "data suggests", "industry reports indicate".

## Frontmatter template

```yaml
---
title: ""                   # 50–60 chars (70 max), primary keyword front-loaded, sentence case
slug: ""                    # ≤60 chars, lowercase-hyphens
silo: ""                    # aeo | seo | marketing-automation | content-marketing
type: ""                    # pillar | guide | article
excerpt: >-                 # 150–160 chars, definitive answer, includes primary keyword, no CTAs
  Direct answer to the primary keyword query.
datePublished: ""           # YYYY-MM-DD (today)
dateModified: ""            # YYYY-MM-DD (today)
author: ""
readingTime: ""             # "X min read" at ~220 wpm
schema:
  - Article
  - ""                      # second type per content
topics: []
targetKeyword: ""
internalLinks: []
relatedContent: []
---
```

The frontmatter `title:` and the in-body `# H1` are separate. The `# H1` is the first body element after the frontmatter close. Exactly one `# H1` per article.

## Length by content type (hard ceiling 7,000 words; never exceed)

| Type | Range |
|---|---|
| Definition / explainer | 2,000–2,800 |
| Comparison | 2,800–3,500 |
| Guide / how-to | 3,500–4,500 |
| Pillar | 4,000–5,000 |

**Aim for the upper half of the range by default.** Under-length is penalized much harder than over-length — thin articles cannot rank and drag the content_length dim toward its floor, whereas a slightly long article pays only a soft structural cost. If the topic supports it, articles over 3,000 words should naturally carry 50+ named entities; over 4,000 words, 65+. If the entity count feels short of that, the article is under-developed — add depth (more H3s, more concrete examples, more practitioner-grade detail) rather than stretching. Don't pad to the ceiling either.

## Structure

- **Paragraphs: 100–250 words** (4–8 sentences typical). Sub-100 gets truncated mid-thought by generative engines; 250+ gets split unpredictably. Answer blocks under AEO headings are the exception (40–70 words).
- **Readability targets (non-negotiable):** aim for Flesch Reading Ease ≥ 50, Gunning Fog ≤ 12, Dale-Chall ≤ 9. These targets require disciplined sentence construction: average sentence length 15–20 words (hard cap 30 words per sentence), prefer one- and two-syllable words over polysyllabic synonyms (use not utilize, help not facilitate, start not commence, get not obtain, need not necessitate, show not demonstrate, enough not sufficient, about not approximately, way not methodology, try not endeavor, end not terminate). Break compound sentences with multiple clauses into two shorter sentences. Open sentences with subject-verb, not subordinate clauses or prepositional chains. A paragraph of six 17-word sentences reads cleaner than three 34-word sentences with embedded clauses.
- **Headings:** one `# H1` with primary keyword, sentence case. 4–10 H2s. 0–6 H3s per H2. Total H2+H3 between 8–27, scaling with length. **For comprehensive topics, aim for the upper half: 15–25 H3s total.** When an H2 covers a large domain (technical SEO, on-page SEO, measurement, local SEO, content strategy), break it into H3 subsections each addressing one canonical concern — one subsection per named concept, not 5 concerns in one paragraph. Never skip levels. Descriptive, question-format headings beat generic labels — prefer "What is X?", "How does X work?", "When should you use X?" over "Overview", "Details".
- **Sentence case everywhere:** first word + proper nouns capitalized; everything else lowercase.
- **Lists:** 1–18 items total across the article. 3–9 per individual list (10+ only if genuinely enumerable). 20+ bullets across an article is an AI tell — prefer prose.
- **Tables:** 0–3 per article. Minimum 3 rows, 2–6 cols. Use for ≥3 items × ≥2 dimensions. One comparison table beats multiple short bullet lists of the same info.
- **Structured element per H2:** **every major H2 section must contain at least one structured element** — a table, numbered or bulleted list, definition block, pros/cons, or comparison — unless the section is pure conceptual explanation where prose reads better. This is non-negotiable for competitive structural density. Aggregate caps (≤3 tables, ≤18 list items) still hold, so distribute the budget across sections rather than clustering.
- **Answer-block cadence:** open **every H2 and every H3 section** with a direct-answer block — 1–3 short sentences (40–70 words total) that stand alone as a complete response to the heading's implied question, using simple declarative sentences under 20 words each. Target: at least 65% of all sections (H2 + H3 combined) open with an extractable answer block. Then elaborate with evidence and depth. This is what generative engines and featured-snippet extractors lift from the page; without it the section looks like narrative throat-clearing to both scorers and AI readers.
- **Keyword placement:** title, H1, first 100 words, at least one H2, conclusion, `excerpt:`. Natural, no stuffing. Vary across H2s — don't repeat the exact keyword form. Secondary keywords across H2s; LSI terms throughout.
- **Character budgets:** title 50–60 (70 max); meta/excerpt 150–160; slug ≤60. Hard constraints, fail-soft.
- **Image placeholders:** `[IMAGE: …]` where visuals help. Soft 0–6 per article.
- **Avoid during generation:** internal links, callout blocks (`> **KEY INSIGHT**:`), pull quotes, related-reading blocks, TL;DR bullets, site-specific formatting. External links to authoritative primary sources are fine when citing a specific stat.

## Entities and breadth

- **30–80 distinct named entities** across the article (50+ over 3,000 words; 65+ over 4,000). "Named entity" = specific tool, product, platform, standard, brand, framework, person, or measurable metric. "Google Business Profile" counts; "business listings" does not.
- **Domain-canonical coverage (critical).** Before writing, mentally enumerate the COMPLETE ecosystem of named entities any expert would expect in a comprehensive treatment of the topic: every major platform, specific tool, named standard, named algorithm or update, measurable metric, industry framework, and named concept. Prioritize covering this canonical roster over introducing novel or tangential entities. A high-entity-count article that misses 60% of the standard entities for its domain scores worse than a smaller article covering 80% of them. For comparison/vs topics, map the entity ecosystem for EACH side independently, then cover both ecosystems throughout the article.
- **Core entities (8–15):** appear in ≥3 separate sections each, in different contexts (introducing, applying, measuring). Each mention should add new information or context, not repeat the same point.
- **Secondary entities (20–30):** appear in ≥2 separate paragraphs each. Single-mention entities contribute breadth but earn zero depth credit. When an entity appears only once, look for a second natural context where it belongs and add it there.
- **Co-occurrence density:** in EVERY paragraph that discusses a concept, name 3–5 specific entities that relate to that concept together. Don't discuss a concept abstractly when you could name the specific tools, platforms, standards, or metrics involved. Related entities must co-occur in the same paragraph, not just the same section. Each H2 contains a natural cluster of 5–8 related entities, with 3–4 mentioned together in a single paragraph. Example: a paragraph about AEO measurement should name Perplexity analytics, Otterly, Profound, GA4, and citation tracking together, not spread them across separate paragraphs.
- **Category coverage:** operational topics span ≥4 of {platforms/tools, standards/frameworks, named metrics, roles/stakeholders, industry-specific entities}; conceptual topics ≥3. Within each category, cover the FULL range of well-known names, not just 1-2 examples.
- **Adjacent-concern questions:** 5–12 covered, scaling with scope.

## Quality floor

1. Substance-first. Every claim earns its place with evidence, data, framework, or specific example.
2. Concrete over abstract. Name the thing, quantify.
3. Position first, proof second, caveats last.
4. Lead with value — article opens and every section opens with claim / data / direct answer. No throat-clearing, no rhetorical-question openers.
5. Every section contributes. Cut filler sections.

## AEO (direct answer extraction)

- **Direct answer first in every section**, then explain. AI answer engines read the first sentences of each section. Bad: 500-word history before the definition. Good: define in the first sentence, then history.
- **Question-format H2/H3s** match how users phrase queries.
- **"What is X?" sections:** open with a 40–70 word stand-alone definition, then elaborate.
- **Comparison sections:** open with the key differentiator.
- **Lists and tables where information is enumerable** — AI extracts structured info more easily than dense prose. Don't manufacture lists from non-list content.
- **FAQ section where questions recur** — Q&A pairs are ideal for AI extraction. Only declare `FAQPage` schema if ≥3 meaningful Q&A pairs exist.
- **Cover follow-up questions** AI Overviews often address.

## EEAT

- **Experience:** concrete examples (specific names, dates, numbers, outcomes), verifiable testing framing.
- **Expertise:** technical accuracy, appropriate depth (2nd/3rd-order considerations), primary sources, correct domain vocabulary without over-defining.
- **Authoritativeness:** clearly stated position where content takes one, confidence backed by evidence rather than hedging.
- **Trustworthiness:** correct facts, cited sources for material claims, no invented statistics or authorities, transparency about limitations.

## GEO (generative-engine citation)

- **Statistics density: 3–5 concrete data points per 1,000 words**, distributed across opening/middle/closing. At least one per major H2. Numbers, percentages, dollar amounts, date-qualified counts, named studies. Never fabricate — use "studies show", "data suggests", "industry reports indicate" when the exact number is unknown.
- **40–60% of major claim sentences carry evidence** (cue phrase or statistic adjacent to the claim). A major claim = any declarative sentence ≥5 words. Below 40% reads as assertion-only prose that engines skip.
- **Claim → evidence → implication** is the most citable unit. Claim sentence. Evidence sentence with stat or named source. Implication sentence with what the reader should do or conclude. Use for every substantive point.
- **Use-case specificity.** "Tool X is a great choice" → "Tool X is best for teams under 50 that need fast onboarding." "X is effective" → "X is effective when the team has ≥3 engineers, data volume >10 GB/day, latency >200ms."
- **Multi-option topics get comparison structure** — table, pros/cons, or decision matrix. AI engines lift these directly into "which should I choose" answers.
- **Bold + italic: 5–10% of body words.** Concentrate on metrics, decision factors, named entities, and section takeaways. Emphasize inside the first sentence of each section — it survives extractive summarisation best. Never bold every noun, every keyword, or every statistic — bold only the one that answers the section's question.
- **Don't over-cite.** Lee (2026) showed citation density beyond a threshold hurts visibility. One or two source references per section beats stuffing.
- **Earned-media framing for commercial topics.** Cite third-party sources, acknowledge trade-offs openly, lead with the problem then options then trade-offs. Brand-first framing loses citation.

## Prose style (humanize)

- **Active voice.** "We shipped on Tuesday" not "It was shipped on Tuesday."
- **Positive form.** "He forgot" not "He did not remember."
- **Concrete specifics** over vague claims. Don't invent specifics if the source was vague.
- **Omit needless words.** in order to → to; due to the fact that → because; has the ability to → can; at this point in time → now; a large number of → many; in the event that → if; despite the fact that → although; is able to → can; prior to → before; for the purpose of → for/to; with regard to → about; it is often the case that → often.
- **One topic per paragraph**, topic sentence first.
- **Emphatic placement:** end-weight. Put the most important phrase at the end of the sentence.
- **Parallel structure** for parallel ideas.
- **Vary sentence rhythm.** Mix short declaratives with longer developing sentences. Same-length sentences across a paragraph is an AI tell.
- **Copula avoidance:** replace "serves as", "stands as", "boasts", "features" (when meaning has), "represents" (when meaning is), "constitutes", "functions as" → use plain `is`/`has`/`includes`.
- **No negative parallelisms.** "It's not just X; it's Y" → "It's Y." "Not only… but also…" → state directly.
- **No forced rule of three.** Don't reflex-triplet for comprehensiveness.
- **No synonym cycling.** Pick a term and stick with it. Don't rotate "protagonist / main character / hero / central figure."
- **No false ranges.** "From X to Y" only when X and Y are on an actual scale.
- **No excessive hedging.** Commit or don't claim.
- **No generic positive conclusions.** Banned endings: "The future looks bright", "Exciting times lie ahead", "This represents a major step", "One thing is clear", "Only time will tell". End with specifics or stop.
- **No formulaic arcs.** "Despite challenges… continues to thrive", "While there are obstacles… the future remains bright", "Future Outlook" vague-optimism sections, "In conclusion" that restates the opening.
- **No vague attributions.** "Experts argue", "Studies have shown", "Observers note" — name the source or cut the claim.
- **No superficial participials.** Ban: "ensuring quality", "highlighting its importance", "showcasing capabilities", "reflecting broader trends", "fostering innovation", "underscoring the need for", "demonstrating a commitment to", "paving the way for". If the info matters, give it a sentence; otherwise drop.

## Formatting

- **No em or en dashes.** Use commas, periods, or parentheses. Hyphen is fine only for actual ranges ("pages 10-15").
- **No emoji.** No emoji bullets, heading decorations, or body emoji.
- **Sentence case headings.**
- **Inline `**Label:** description` bullets** work when labels are distinct and descriptions are ≤1 sentence. Avoid when labels are forced, descriptions run 2+ sentences, or >5 items.
- **Formatting restraint:** bullets only for enumerable items; tables only for comparisons (3+ items × 2+ dims); numbered lists only for sequential processes.
- **Prefer flowing prose** for explanations.

## Introduction and conclusion

- **Intro:** hook (claim / stat / direct answer), brief problem statement, promise of what the article delivers. Primary keyword in first 100 words. No "in today's digital landscape", no rhetorical-question opener, no generic "SEO is important."
- **Conclusion:** concise recap. Don't restate the intro verbatim. Primary keyword once more if it fits naturally. No "In conclusion" / "To summarize" openings.

## Banned words — zero tolerance (scorer deducts 0.03 per hit)

### AI vocabulary
delve; tapestry; fostering; foster; garner; multifaceted; intricate; intricacies; realm; leverage; showcase; underscore; underscores; testament; vibrant; interplay; seamless; seamlessly; robust; transformative; pivotal; crucial.

### Filler constructions (cut entirely)
in today's world; in today's fast-paced world; in today's landscape; in today's digital landscape; in today's marketing landscape; in today's SEO landscape; in the age of AI; it's worth noting that; it goes without saying; needless to say; moving forward; at the end of the day; it is important to note that; it's important to note; it should be noted that; it is worth mentioning that; there is no denying that; it cannot be overstated that; let's unpack.

### Puffery adjectives
groundbreaking; game-changing; game-changer; revolutionary; monumental; unprecedented; disruptive.

### Marketing speak
cutting-edge; best-in-class; world-class; next-generation; state-of-the-art; unparalleled; one-of-a-kind; supercharge; turbocharge; unlock; 10x; hack; secret; trick.

### Copula substitutes (soft — penalty only when used instead of `is`/`has`)
serves as; stands as; boasts; functions as.

### Paragraph-start bans (fine mid-sentence, banned as paragraph openers)
additionally; moreover; furthermore.

### Transitional bans (at sentence boundaries only)
to summarize; in conclusion.

### Significance puffery (rewrite)
"stands as a testament to"; "marks a pivotal moment"; "reflects broader trends"; "setting the stage for"; "a vital role in"; "represents a significant step"; "a major milestone".

## Content-type skeletons (use as scaffolds, not fill-in-the-blank forms)

**Blog / definition:** H1 → intro → "What is X?" (40–70w answer + context) → "Why X matters" (benefits + evidence) → main sections with H3s → "Common mistakes to avoid" → FAQ (3+ Qs) → conclusion.

**Comparison:** H1 → intro + quick-answer verdict → "A vs B: quick comparison" (table) → "What is A?" (+pros/cons) → "What is B?" (+pros/cons) → "Detailed comparison" (H3 per feature category, each ending with "Winner: X") → Pricing A / Pricing B → "Which should you choose?" (Choose A if / Choose B if) → FAQ → "Final verdict".

**How-to:** H1 → intro + outcome + timeframe → "What you'll need" (tools, time, skill level) → "Why X matters" → "Step 1: …" through "Step N: …" (each with instruction, why, sub-steps, common mistakes) → "What to do if [common problem]" → "Advanced tips" → FAQ → conclusion.

**Listicle:** H1 with year → intro + criteria → "Quick summary" table → "How we chose these" → "1. [Item] — best for [use case]" through "N. [Item]" (each with what it is, key features, pricing, pros, cons, best for) → "Comparison" table → "How to choose" (factors) → FAQ → "Our top pick".

**Review:** H1 with year → intro with testing framing → overview (what it is, price, best for, rating) → pros/cons at a glance → "What is [product]" → "Key features" (H3 per feature) → "Performance testing" (H3 per test category with methodology, results, verdict) → "Pricing and plans" (table) → "[Product] vs competitors" table → "Who should buy" (perfect for / not ideal for) → FAQ → "Final verdict" with rating.

**Pillar:** H1 with year → intro → "Table of contents" → "Chapter 1: Fundamentals" (what is / why matters / key concepts table) → chapters 2-N covering major sub-topics with H3s → FAQ (8–10) → "Key takeaways" → "What's next?" → "Resources".

**FAQ:** H1 → intro → "General questions" → "Getting started" → "Common problems" → "Comparison questions" → "Pricing and plans" → "Advanced questions". Each section contains 2–4 Qs with 40–70w answers.

## Completion

End the file with this line on its own:

```
<!-- phase-4: pass -->
```

Without the sentinel the harness treats the generation as failed. Emit no narration.
