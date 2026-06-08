import time
import re
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
import feedparser
import gspread
from google.oauth2.service_account import Credentials
import json
from config import SOURCES, SKIP_KEYWORDS, FUNDING_TITLE_KEYWORDS, MAX_ARTICLES_PER_SOURCE, SHEETS_ID, GOOGLE_CREDENTIALS_JSON

logger = logging.getLogger(__name__)

ONE_WEEK_AGO   = datetime.now(timezone.utc) - timedelta(days=7)
NINETY_DAYS_AGO = datetime.now(timezone.utc) - timedelta(days=90)

def _parse_pub_date(entry) -> datetime | None:
    """Return the publication datetime from an RSS entry, or None if unavailable."""
    parsed = None
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        parsed = entry.published_parsed
    elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
        parsed = entry.updated_parsed
    if parsed is None:
        return None
    try:
        return datetime(*parsed[:6], tzinfo=timezone.utc)
    except Exception:
        return None

def is_within_one_week(entry) -> bool:
    pub_dt = _parse_pub_date(entry)
    if pub_dt is None:
        return True  # no date → let through, AI processor will catch very old ones
    return pub_dt >= ONE_WEEK_AGO

def _normalize_title(title: str) -> str:
    """Lowercase + remove punctuation for similarity comparison."""
    return re.sub(r"[\W_]+", " ", title.lower()).strip()

def _is_duplicate_title(title: str, seen_titles: list[str], threshold: float = 0.82) -> bool:
    norm = _normalize_title(title)
    for seen in seen_titles:
        if SequenceMatcher(None, norm, seen).ratio() >= threshold:
            return True
    return False

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def get_sheets_client():
    if not GOOGLE_CREDENTIALS_JSON:
        raise EnvironmentError("GOOGLE_CREDENTIALS_JSON secret is not set or is empty.")
    creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    return gspread.authorize(creds)


def get_or_create_sheet(gc: gspread.Client, tab_name: str) -> gspread.Worksheet:
    ss = gc.open_by_key(SHEETS_ID)
    try:
        return ss.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=tab_name, rows=2000, cols=8)
        ws.append_row(["url", "title", "content", "source", "region", "fetchedAt", "processed", "publishedAt"])
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


def has_funding_keyword(title: str) -> bool:
    t = title.lower()
    return any(kw.lower() in t for kw in FUNDING_TITLE_KEYWORDS)


def scrape_source(source: dict, ws: gspread.Worksheet, existing_urls: set,
                  seen_titles: list[str], articles: list[dict] | None = None) -> int:
    if articles is None:
        articles = fetch_rss(source["rss"])
    rows_batch = []
    logger.info("  %s: found %d articles", source["name"], len(articles))
    saved = skipped_dup = skipped_kw = 0

    for article in articles:
        if saved >= MAX_ARTICLES_PER_SOURCE:
            break
        if article["url"] in existing_urls:
            continue
        if should_skip(article["title"]):
            skipped_kw += 1
            logger.debug("  Skip (keyword): %s", article["title"][:50])
            continue
        if source.get("require_funding") and not has_funding_keyword(article["title"]):
            skipped_kw += 1
            logger.debug("  Skip (no funding keyword): %s", article["title"][:50])
            continue
        if _is_duplicate_title(article["title"], seen_titles):
            skipped_dup += 1
            logger.debug("  Skip (duplicate title): %s", article["title"][:50])
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
            article.get("publishedAt", ""),   # col 8: publishedAt for AI processor date-filter
        ])
        existing_urls.add(article["url"])
        seen_titles.append(_normalize_title(article["title"]))
        saved += 1

    if rows_batch:
        ws.append_rows(rows_batch, value_input_option="RAW")
        time.sleep(1.5)
    logger.info("  Saved %d | dup_skip=%d kw_skip=%d", saved, skipped_dup, skipped_kw)
    return saved


def run_all_scrapers(tab_name: str | None = None) -> str:
    if tab_name is None:
        tab_name = "raw_" + datetime.now().strftime("%Y-%m-%d")

    gc = get_sheets_client()
    ws = get_or_create_sheet(gc, tab_name)
    existing_urls = get_existing_urls(ws)
    seen_titles: list[str] = []  # cross-source title dedup within this run

    enabled = [s for s in SOURCES if s["enabled"]]
    logger.info("runAllScrapers: %d sources → sheet: %s", len(enabled), tab_name)

    # Fetch all RSS feeds concurrently (network I/O bound)
    prefetched: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        future_to_source = {
            executor.submit(fetch_rss, source["rss"]): source for source in enabled
        }
        for future in as_completed(future_to_source):
            source = future_to_source[future]
            try:
                prefetched[source["id"]] = future.result()
                logger.info("📰 %s [%s]: %d articles", source["name"], source["region"], len(prefetched[source["id"]]))
            except Exception as e:
                logger.error("❌ %s fetch: %s", source["id"], e)
                prefetched[source["id"]] = []

    # Write to Sheets sequentially to preserve dedup order and respect rate limits
    for source in enabled:
        try:
            scrape_source(source, ws, existing_urls, seen_titles, prefetched.get(source["id"]))
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
    seen_titles: list[str] = []

    sources = [s for s in SOURCES if s["enabled"] and s["region"] == region]
    logger.info("scrape [%s]: %d sources", region, len(sources))

    for source in sources:
        try:
            scrape_source(source, ws, existing_urls, seen_titles)
        except Exception as e:
            logger.error("❌ %s: %s", source["id"], e)

    return tab_name
