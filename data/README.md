# Data

- `keywords.json` lists the 4 test keywords, their 3 public competitor URLs each, and the content file.
- `competitors/` (scraped markdown) and `profiles/` (derived JSON, which embeds full competitor text) are gitignored because they contain third-party copyrighted articles.
- Rebuild locally with `python scripts/scrape_competitors.py` and then `python scripts/analyze_competitors.py`.
- The profile build needs an NVIDIA GPU and `GEMINI_API_KEY` (for the GEO simulation queries).
