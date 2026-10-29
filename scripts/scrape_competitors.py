#!/usr/bin/env python3
"""Scrape competitor URLs using crawl4ai and save as structured markdown."""

import asyncio
import json
import os
import re
from datetime import date

from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, BrowserConfig

URLS = {
    "search-intent": {
        "keyword": "what is search intent",
        "competitors": {
            "yoast": "https://yoast.com/search-intent/",
            "semrush": "https://www.semrush.com/blog/search-intent/",
            "seerinteractive": "https://www.seerinteractive.com/insights/what-is-search-intent",
        },
    },
    "seo-for-saas": {
        "keyword": "seo for saas",
        "competitors": {
            "marketermilk": "https://www.marketermilk.com/blog/saas-seo",
            "growandconvert": "https://www.growandconvert.com/seo/saas-seo-strategy/",
            "designrevision": "https://designrevision.com/blog/seo-for-saas",
        },
    },
    "aeo-vs-seo": {
        "keyword": "aeo vs seo",
        "competitors": {
            "optimizely": "https://www.optimizely.com/insights/blog/seo-vs-aeo/",
            "seo-com": "https://www.seo.com/ai/aeo-vs-seo/",
            "mavlers": "https://www.mavlers.com/blog/seo-vs-aeo/",
        },
    },
    "seo-for-roofing": {
        "keyword": "seo for roofing",
        "competitors": {
            "servicetitan": "https://www.servicetitan.com/blog/roofing-seo",
            "magnifylab": "https://www.magnifylab.com/blog/seo-for-roofing-companies-guide/",
            "backlinko": "https://backlinko.com/seo-for-roofers",
        },
    },
}

BASE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "competitors")
TODAY = date.today().isoformat()


def count_media(markdown: str) -> dict:
    """Count media references in the markdown output."""
    images = len(re.findall(r'!\[.*?\]\(.*?\)', markdown))
    # Also count <img> tags that crawl4ai might leave
    images += len(re.findall(r'<img\s', markdown, re.IGNORECASE))
    tables = len(re.findall(r'\|.*\|.*\|', markdown)) // 2  # rough: count rows/2
    videos = len(re.findall(r'(youtube|vimeo|wistia|<video|<iframe)', markdown, re.IGNORECASE))
    return {"images": images, "videos": videos, "tables": tables}


def count_words(markdown: str) -> int:
    """Count words in markdown, stripping markup."""
    # Remove image references, links, headers markers
    text = re.sub(r'!\[.*?\]\(.*?\)', '', markdown)
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    text = re.sub(r'#{1,6}\s+', '', text)
    text = re.sub(r'[|*_`~>-]', ' ', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'https?://\S+', '', text)
    words = [w for w in text.split() if len(w) > 0]
    return len(words)


async def scrape_all():
    browser_config = BrowserConfig(
        headless=True,
        text_mode=False,  # Keep images in output for media tracking
    )

    run_config = CrawlerRunConfig(
        wait_until="networkidle",
        page_timeout=30000,
        scan_full_page=True,        # Scroll to load lazy content
        scroll_delay=0.5,
    )

    async with AsyncWebCrawler(config=browser_config) as crawler:
        for kw_slug, kw_data in URLS.items():
            keyword = kw_data["keyword"]
            out_dir = os.path.join(BASE_DIR, kw_slug)
            os.makedirs(out_dir, exist_ok=True)

            for site_name, url in kw_data["competitors"].items():
                print(f"  Scraping: {kw_slug}/{site_name} — {url}")
                try:
                    result = await crawler.arun(url=url, config=run_config)

                    if not result.success:
                        print(f"    FAILED: {result.error_message}")
                        continue

                    md = result.markdown or ""
                    word_count = count_words(md)
                    media = count_media(md)

                    # Build output file with frontmatter
                    output = f"""---
source_url: {url}
keyword: {keyword}
scraped_date: {TODAY}
word_count: {word_count}
images: {media['images']}
videos: {media['videos']}
tables: {media['tables']}
---

{md}
"""
                    out_path = os.path.join(out_dir, f"{site_name}.md")
                    with open(out_path, "w") as f:
                        f.write(output)

                    print(f"    OK: {word_count:,} words, {media['images']} imgs, {media['videos']} vids, {media['tables']} tbls")

                except Exception as e:
                    print(f"    ERROR: {e}")


if __name__ == "__main__":
    asyncio.run(scrape_all())
