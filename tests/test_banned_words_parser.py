"""
Regression net for banned-words.md format.

evaluate.py:load_banned_words() parses the file by section header. If a future
edit drops a section or changes the header wording, scoring silently stops
penalizing that category. These assertions pin the format.
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))


def test_banned_words_loads_nonempty():
    from evaluate import BANNED_WORDS, PARAGRAPH_START_BANS
    assert isinstance(BANNED_WORDS, list)
    assert isinstance(PARAGRAPH_START_BANS, list)
    assert len(BANNED_WORDS) > 10, "banned-words list unexpectedly small"
    assert len(PARAGRAPH_START_BANS) >= 3, "paragraph-start bans should include additionally/moreover/furthermore"


def test_canonical_ai_vocab_present():
    """A handful of hall-of-fame AI tells must stay in the list."""
    from evaluate import BANNED_WORDS
    for word in ("delve", "tapestry", "leverage", "seamless", "robust"):
        assert word in BANNED_WORDS, f"expected {word!r} in banned list"


def test_paragraph_start_bans_exclusive():
    """'additionally', 'moreover', 'furthermore' are banned ONLY as paragraph
    starters. They must not appear in the global BANNED_WORDS list or they'd
    be double-counted."""
    from evaluate import BANNED_WORDS, PARAGRAPH_START_BANS
    for w in ("additionally", "moreover", "furthermore"):
        assert w in PARAGRAPH_START_BANS
        assert w not in BANNED_WORDS, (
            f"{w!r} appears in BANNED_WORDS; it should be paragraph-start-only "
            "(evaluate.py:load_banned_words has explicit logic for this separation)"
        )


def test_hard_refusals_not_scored():
    """Hard refusals are voice-choice directives, not scored word bans.
    Ensure canonical refusal phrasing isn't accidentally in the scored list."""
    from evaluate import BANNED_WORDS
    # Hard refusals describe positions, not words. None of the following
    # fragments should be in the scored substring list.
    for fragment in ("overnight rankings", "guaranteed rankings", "ai will replace seo"):
        assert fragment not in BANNED_WORDS


def test_file_still_has_expected_sections():
    """If someone renames a section header, the parser silently drops it.
    This pins the header text."""
    path = PROJECT_ROOT / "rules" / "content-generation" / "banned-words.md"
    text = path.read_text(encoding="utf-8")
    for header in (
        "## AI Vocabulary",
        "## Filler Constructions",
        "## Puffery Adjectives",
        "## Marketing Speak",
        "## Paragraph-Start Bans",
        "## Transitional Bans",
    ):
        assert header in text, f"banned-words.md missing section header: {header!r}"
