import time
import re
import logging
from datetime import datetime, timezone, timedelta
import feedparser
import requests
import gspread
from google.oauth2.service_account import Credentials
import json
from config import SOURCES, SKIP_KEYWORDS, MAX_ARTICLES_PER_SOURCE, SHEETS_ID, GOOGLE_CREDENTIALS_JSON

logger = logging.getLogger(__name__)

ONE_WEEK_AGO = datetime.now(timezone.utc) - timedelta(days=7)

def is_within_one_week(entry) -> bool:
    pub=None
    if hasattr(entry,"published_parsed") and entry.published_parsed: pub=entry.published_parsed
    elif hasattr(entry,"updated_parsed") and entry.updated_parsed: pub=entry.updated_parsed
    if pub is None: return True
    try:
        pub_dt=datetime(*pub[:6],tzinfo=timezone.utc)
        return pub_dt >= ONE_WEEK_AGO
    except: return True

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def get_sheets_client():
    creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    return gspread.authorize(creds)


def get_or_create_sheet(gc: gspread.Client, tab_name: str) -> gspread.Worksheet:
    ss = gc.open_by_key(SHEETS_ID)
    try:
        return ss.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=tab_name, rows=2000, cols=7)
        ws.append_row(["url", "title", "content", "source", "region", "fetchedAt", "processed"])
        ws.freeze(rows=1)
        logger.info("Created sheet tab: %s", tab_name)
        return ws


def get_existing_urls(ws: gspread.Worksheet) -> set:
    col = ws.col_values(1)  # 第1欄 = url
    return set(col[1:])     # 跳過 header


def fetch_rss(url: str) -> list[dict]:
    try:
        feed = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0 (compatible; FeedBot/1.0)"})
        articles = []
        for entry in feed.entries:
            if not is_within_one_week(entry): continue
            link = getattr(entry, "link", "") or ""
            title = getattr(entry, "title", "") or ""
            summary = getattr(entry, "summary", "") or ""
            content = ""
            if hasattr(entry, "content") and entry.content:
                content = entry.content[0].get("value", "") or ""
            published = getattr(entry, "published", "") or ""
            if link:
                articles.append({
                    "url": link.strip(),
                    "title": title.strip(),
                    "description": summary.strip(),
                    "content": content.strip(),
                    "publishedAt": published,
                })
        return articles
    except Exception as e:
        logger.error("fetchRSS %s: %s", url, e)
        return []


def clean_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def should_skip(title: str) -> bool:
    t = title.lower()
    return any(kw.lower() in t for kw in SKIP_KEYWORDS)


def scrape_source(source: dict, ws: gspread.Worksheet, existing_urls: set) -> int:
    articles = fetch_rss(source["rss"])
    rows_batch = []
    logger.info("  %s: found %d articles", source["name"], len(articles))
    saved = 0
    for article in articles:
        if saved >= MAX_ARTICLES_PER_SOURCE:
            break
        if article["url"] in existing_urls:
            continue
        if should_skip(article["title"]):
            logger.debug("  Skip (keyword): %s", article["title"][:50])
            continue

        content = "\n\n".join(filter(None, [
            f"標題：{article['title']}",
            f"摘要：{article['description']}",
            f"內文：{clean_html(article['content'])[:3000]}" if article["content"] else "",
        ]))

        rows_batch.append([
            article["url"],
            article["title"],
            content,
            source["id"],
            source["region"],
            datetime.now(timezone.utc).isoformat(),
            "false",
        ])
        existing_urls.add(article["url"])
        saved += 1

    if rows_batch:
        ws.append_rows(rows_batch, value_input_option="RAW")
        time.sleep(1.5)
    logger.info("  Saved %d new articles", saved)
    return saved


def run_all_scrapers(tab_name: str | None = None) -> str:
    if tab_name is None:
        tab_name = "raw_" + datetime.now().strftime("%Y-%m-%d")

    gc = get_sheets_client()
    ws = get_or_create_sheet(gc, tab_name)
    existing_urls = get_existing_urls(ws)

    enabled = [s for s in SOURCES if s["enabled"]]
    logger.info("runAllScrapers: %d sources → sheet: %s", len(enabled), tab_name)

    for source in enabled:
        try:
            logger.info("📰 %s [%s]", source["name"], source["region"])
            scrape_source(source, ws, existing_urls)
        except Exception as e:
            logger.error("❌ %s: %s", source["id"], e)

    logger.info("✅ runAllScrapers done")
    return tab_name


def run_scraper_by_region(region: str, tab_name: str | None = None) -> str:
    if tab_name is None:
        tab_name = "raw_" + datetime.now().strftime("%Y-%m-%d")

    gc = get_sheets_client()
    ws = get_or_create_sheet(gc, tab_name)
    existing_urls = get_existing_urls(ws)

    sources = [s for s in SOURCES if s["enabled"] and s["region"] == region]
    logger.info("scrape [%s]: %d sources", region, len(sources))

    for source in sources:
        try:
            scrape_source(source, ws, existing_urls)
        except Exception as e:
            logger.error("❌ %s: %s", source["id"], e)

    return tab_name
