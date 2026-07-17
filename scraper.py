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
from config import SOURCES, SKIP_KEYWORDS, FUNDING_TITLE_KEYWORDS, MAX_ARTICLES_PER_SOURCE, SHEETS_ID, GOOGLE_CREDENTIALS_JSON, REGION_AI_TARGET

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


def fetch_rss(url: str, retries: int = 2, retry_delay: float = 3.0) -> list[dict]:
    try:
        feed = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0 (compatible; FeedBot/1.0)"})
        # news.google.com search feeds intermittently come back with 0 entries (no error,
        # no bozo flag — Google just serves an empty result set to some requesters) and
        # succeed a few seconds later on retry. Confirmed on 2026-07-17: GitHub Actions CI
        # got 0 entries for a bnext.com.tw query three weeks running, while the same URL
        # returned 92 entries seconds apart from a different network. Only retry Google
        # News URLs — a real 403/404 on a normal feed already raises before we get here.
        attempt = 0
        while not feed.entries and "news.google.com" in url and attempt < retries:
            attempt += 1
            logger.warning("  %s returned 0 entries — retry %d/%d", url, attempt, retries)
            time.sleep(retry_delay)
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


def _build_row(article: dict, source: dict) -> list:
    content = "\n\n".join(filter(None, [
        f"標題：{article['title']}",
        f"摘要：{article['description']}",
        f"內文：{clean_html(article['content'])[:3000]}" if article["content"] else "",
    ]))
    return [
        article["url"],
        article["title"],
        content,
        source["id"],
        source["region"],
        datetime.now(timezone.utc).isoformat(),
        "false",
        article.get("publishedAt", ""),   # col 8: publishedAt for AI processor date-filter
    ]


def scrape_source(source: dict, ws: gspread.Worksheet, existing_urls: set,
                  seen_titles: list[str], articles: list[dict] | None = None,
                  backup: list[list] | None = None) -> int:
    """backup: 若給定，require_funding 濾掉的文章不會被丟棄，而是組成候補列（row）
    放進這個 list，供呼叫端在該地區篇數不足時（REGION_AI_TARGET）補進 sheet。"""
    if articles is None:
        articles = fetch_rss(source["rss"])
    rows_batch = []
    logger.info("  %s: found %d articles", source["name"], len(articles))
    saved = skipped_dup = skipped_kw = backed_up = 0

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
            if backup is not None and not _is_duplicate_title(article["title"], seen_titles):
                backup.append(_build_row(article, source))
                existing_urls.add(article["url"])
                seen_titles.append(_normalize_title(article["title"]))
                backed_up += 1
            continue
        if _is_duplicate_title(article["title"], seen_titles):
            skipped_dup += 1
            logger.debug("  Skip (duplicate title): %s", article["title"][:50])
            continue

        rows_batch.append(_build_row(article, source))
        existing_urls.add(article["url"])
        seen_titles.append(_normalize_title(article["title"]))
        saved += 1

    if rows_batch:
        ws.append_rows(rows_batch, value_input_option="RAW")
        time.sleep(1.5)
    logger.info("  Saved %d | dup_skip=%d kw_skip=%d backup=%d", saved, skipped_dup, skipped_kw, backed_up)
    return saved


def _rank_backup(backup: list[list]) -> list[list]:
    """把候補列依 ai_processor.rule_classify 的判斷排序：'startup'（AI 大機率會收）優先，
    'ambiguous' 次之（一樣會送 AI 判斷），rule_classify 判 'skip' 的直接丟掉——那些補了也不
    會真的被送去給 AI（Stage0 會免費濾掉），對「送 AI 篇數」的目標沒有幫助。"""
    from ai_processor import rule_classify
    buckets: dict[str, list[list]] = {"startup": [], "ambiguous": []}
    for row in backup:
        title = row[1] if len(row) > 1 else ""
        verdict = rule_classify(title)
        if verdict in buckets:
            buckets[verdict].append(row)
    return buckets["startup"] + buckets["ambiguous"]


def _top_up_region(ws: gspread.Worksheet, region: str, backup: list[list]) -> None:
    target = REGION_AI_TARGET.get(region)
    if not target or not backup:
        return
    rows = ws.get_all_values()
    region_count = sum(1 for r in rows[1:] if len(r) > 4 and r[4].strip() == region)
    gap = target - region_count
    if gap <= 0:
        logger.info("🔁 [%s] already at target (%d/%d) — no top-up needed", region, region_count, target)
        return
    ranked = _rank_backup(backup)
    top_up = ranked[:gap]
    if not top_up:
        logger.warning("🔁 [%s] below target (%d/%d) but no viable candidates in backlog (all rule-skip)",
                       region, region_count, target)
        return
    ws.append_rows(top_up, value_input_option="RAW")
    time.sleep(1.5)
    logger.info("🔁 [%s] below target (%d/%d) — topped up %d from require_funding backlog (had %d candidates)",
                region, region_count, target, len(top_up), len(backup))


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
    backups_by_region: dict[str, list[list]] = {r: [] for r in REGION_AI_TARGET}
    for source in enabled:
        try:
            scrape_source(source, ws, existing_urls, seen_titles, prefetched.get(source["id"]),
                          backup=backups_by_region.get(source["region"]))
        except Exception as e:
            logger.error("❌ %s: %s", source["id"], e)

    for region, backup in backups_by_region.items():
        _top_up_region(ws, region, backup)

    logger.info("✅ runAllScrapers done")
    return tab_name


def run_scraper_by_region(region: str, tab_name: str | None = None) -> str:
    if tab_name is None:
        tab_name = "raw_" + datetime.now().strftime("%Y-%m-%d")

    gc = get_sheets_client()
    ws = get_or_create_sheet(gc, tab_name)
    existing_urls = get_existing_urls(ws)
    seen_titles: list[str] = []
    backup: list[list] = []

    sources = [s for s in SOURCES if s["enabled"] and s["region"] == region]
    logger.info("scrape [%s]: %d sources", region, len(sources))

    for source in sources:
        try:
            scrape_source(source, ws, existing_urls, seen_titles, backup=backup)
        except Exception as e:
            logger.error("❌ %s: %s", source["id"], e)

    _top_up_region(ws, region, backup)

    return tab_name
