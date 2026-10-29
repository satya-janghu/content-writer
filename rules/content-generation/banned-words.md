# Banned Words (Scorable List)

Authoritative, scorer-parseable list of banned words/phrases. Each hit deducts 0.03 from the composite score (see `scripts/evaluate.py` dimension #10).

This file is **parsed by the scorer** — format matters. Use `- item` bullets under the `## ` section headers below. Do not reorganize the section headings without also updating `scripts/evaluate.py:load_banned_words()`.

Human-facing prose rules about voice, register, and quality floor live in `SKILL.md`. This file is the scorer-parseable data (evaluate.py reads it directly).

## AI Vocabulary

Words that signal AI authorship and rarely carry irreplaceable meaning.

- delve
- tapestry
- fostering
- foster
- garner
- multifaceted
- intricate
- intricacies
- realm
- leverage
- showcase
- underscore
- underscores
- testament
- vibrant
- interplay
- seamless
- seamlessly
- robust
- transformative
- pivotal
- crucial

## Filler Constructions

Phrases that pad text without adding meaning.

- in today's world
- in today's fast-paced world
- in today's landscape
- in today's digital landscape
- in today's marketing landscape
- in today's SEO landscape
- in the age of AI
- it's worth noting that
- it goes without saying
- needless to say
- moving forward
- at the end of the day
- it is important to note that
- it's important to note
- it should be noted that
- it is worth mentioning that
- there is no denying that
- it cannot be overstated that
- let's unpack

## Puffery Adjectives

- groundbreaking
- game-changing
- game-changer
- revolutionary
- monumental
- unprecedented
- disruptive

## Marketing Speak

- cutting-edge
- best-in-class
- world-class
- next-generation
- state-of-the-art
- unparalleled
- one-of-a-kind
- supercharge
- turbocharge
- unlock
- 10x
- hack
- secret
- trick

## Copula Substitutes

Soft bans — penalty only when used instead of simple `is`/`has`.

- serves as
- stands as
- boasts
- functions as

## Paragraph-Start Bans

Banned only as paragraph openers, not mid-sentence.

- additionally
- moreover
- furthermore

## Transitional Bans

Banned only at sentence boundaries.

- to summarize
- in conclusion

## Hard Refusals

These are editorial (not scored as banned words — they're absolute prohibitions on the *position* content takes). See `SKILL.md` (Hard refusals section) for the full list and rationale.

- Never claim overnight rankings.
- Never take "AI will replace SEO" positions.
- Never fabricate case studies, numbers, or named-person quotes.
