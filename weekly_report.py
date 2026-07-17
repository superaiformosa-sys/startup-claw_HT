"""
新創周報產生器
從 Google Sheets 讀取本週資料，產出終端機彩色報告 + HTML 檔案
Usage:
    python weekly_report.py                   # 用今天的 tab
    python weekly_report.py raw_2026-05-20    # 指定 tab
    python weekly_report.py --html            # 同時輸出 HTML
"""
import sys
import html as _html
import json
import logging
import re
import datetime
from collections import Counter
from ai_processor import get_sheet
from config import (
    SOURCES, REGION_EMOJI, INDUSTRY_COLOR, STAGE_ORDER, REGION_ORDER,
    MIN_DISPLAY_GROUP_FIT, REGION_DISPLAY_MAX, REGION_DISPLAY_MIN,
    SEA_EXCLUDE_SOURCES, INDUSTRY_KEYWORDS, TAG_LABELS,
)

logger = logging.getLogger(__name__)

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.columns import Columns
from rich.text import Text
from rich.rule import Rule
from rich import box
from rich.align import Align

console = Console(width=110)


# ── Data loading ──

def _current_week_start(today: datetime.date) -> datetime.date:
    """本週的週日（Sun-Sat 週期的第一天）。"""
    days_since_sunday = (today.weekday() + 1) % 7
    return today - datetime.timedelta(days=days_since_sunday)


def load_all_tabs(gc_client=None) -> list[dict]:
    """載入 Google Sheets 所有 raw_* tab 的資料（本週日~今天）"""
    import gspread
    from google.oauth2.service_account import Credentials
    from config import SHEETS_ID, GOOGLE_CREDENTIALS_JSON

    SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(json.loads(GOOGLE_CREDENTIALS_JSON), scopes=SCOPES)
    gc = gspread.authorize(creds)
    ss = gc.open_by_key(SHEETS_ID)

    today      = datetime.date.today()
    week_start = _current_week_start(today)
    rows = []

    for ws in ss.worksheets():
        if not ws.title.startswith("raw_"):
            continue
        try:
            date_str = ws.title.replace("raw_", "")
            tab_date = datetime.date.fromisoformat(date_str)
            if tab_date < week_start:
                continue
        except ValueError:
            continue
        data = ws.get_all_values()
        if len(data) <= 1:
            continue
        for row in data[1:]:
            if len(row) < 7:
                continue
            rows.append({
                "url": row[0], "title": row[1], "content": row[2],
                "source": row[3], "region": row[4],
                "fetchedAt": row[5], "processed": row[6],
                "tab": ws.title,
            })
    return rows


def load_single_tab(tab_name: str) -> list[dict]:
    ws = get_sheet(tab_name)
    data = ws.get_all_values()
    rows = []
    if len(data) <= 1:
        return rows
    for row in data[1:]:
        if len(row) < 5:
            continue
        rows.append({
            "url": row[0], "title": row[1], "content": row[2],
            "source": row[3] if len(row) > 3 else "",
            "region": row[4] if len(row) > 4 else "",
            "fetchedAt": row[5] if len(row) > 5 else "",
            "processed": row[6] if len(row) > 6 else "false",
            "tab": tab_name,
        })
    return rows


def load_scored_map(collection: str) -> dict:
    """從 Firebase 讀取近 7 天所有 startups_* 集合的已評分文章，以 sourceUrl 為 key。
    掃描多個集合確保不因日期差異遺漏資料。"""
    try:
        from firebase_client import get_db
        import datetime as _dt
        db     = get_db()
        result = {}
        today  = _dt.date.today()
        # Scan Firebase collections from this week's Sunday to today (Sun-Sat week),
        # plus always the specific collection being reported on — the week window alone
        # misses it whenever the report is (re)generated outside the batch's own week
        # (e.g. a Friday batch resent the following Monday).
        days_since_sunday = (today.weekday() + 1) % 7
        col_names = {
            "startups_" + (today - _dt.timedelta(days=delta)).isoformat()
            for delta in range(days_since_sunday + 1)
        }
        col_names.add(collection)
        for col_name in col_names:
            try:
                for doc in db.collection(col_name).stream():
                    d = doc.to_dict()
                    url = d.get("sourceUrl", "")
                    if url and url not in result:
                        # Normalise: use fitScore as hotaiFitScore fallback for pre-field docs
                        if d.get("hotaiFitScore") is None and d.get("fitScore") is not None:
                            d["hotaiFitScore"] = d["fitScore"]
                        result[url] = d
            except Exception:
                pass  # collection may not exist for this date
        logger.info("load_scored_map: %d total scored docs (last 7 days)", len(result))
        return result
    except Exception as e:
        logger.warning("load_scored_map failed: %s", e)
        return {}


# ── Article title analysis (no AI needed) ──

def guess_industry(title: str, content: str) -> list[str]:
    combined = (title + " " + content[:500]).lower()
    found = [cat for cat, kws in INDUSTRY_KEYWORDS.items() if any(kw in combined for kw in kws)]
    return found[:3] if found else ["其他"]


def parse_funding_from_title(title: str) -> str:
    patterns = [
        r"([\d.]+\s*億[美台人]?幣?)",
        r"([\d.]+\s*萬[美台人]?幣?)",
        r"(\$[\d.]+[MmBb])",
        r"(USD?\s*[\d.]+[MmBb])",
        r"([\d.]+\s*million)",
        r"(融資[\d.億萬]+)",
        r"(募資[\d.億萬]+)",
    ]
    for p in patterns:
        m = re.search(p, title, re.IGNORECASE)
        if m:
            return m.group(1)
    return ""


def guess_stage_from_title(title: str) -> str:
    stage_map = {
        "種子輪": ["種子輪", "seed"],
        "天使輪": ["天使輪", "angel"],  # Bug8 fix: was incorrectly grouped under 種子輪
        "Pre-A": ["pre-a", "prea", "pre a"],
        "A輪": ["a輪", "series a", "a round"],
        "B輪": ["b輪", "series b", "b round"],
        "C輪": ["c輪", "series c"],
        "D輪": ["d輪", "series d"],
        "戰略投資": ["戰略投資", "strategic"],
        "IPO": ["ipo", "上市", "掛牌"],
    }
    t = title.lower()
    for stage, kws in stage_map.items():
        if any(kw in t for kw in kws):
            return stage
    return ""


# ── Stats & analysis ──

def analyze_rows(rows: list[dict]) -> dict:
    region_count = Counter()
    source_count = Counter()
    industry_count = Counter()
    stage_count = Counter()
    processed_count = Counter()
    funding_articles = []
    notable = []

    source_map = {s["id"]: s["name"] for s in SOURCES}

    for r in rows:
        region = r.get("region", "未知")
        region_count[region] += 1

        src_id = r.get("source", "")
        source_count[source_map.get(src_id, src_id)] += 1

        proc = r.get("processed", "false").lower()
        processed_count[proc] += 1

        title = r.get("title", "")
        content = r.get("content", "")
        industries = guess_industry(title, content)
        for ind in industries:
            industry_count[ind] += 1

        stage = guess_stage_from_title(title)
        if stage:
            stage_count[stage] += 1

        funding = parse_funding_from_title(title)
        if funding:
            funding_articles.append({
                "title": title[:70],
                "funding": funding,
                "region": region,
                "stage": stage,
                "url": r.get("url", ""),
            })

        if any(kw in title.lower() for kw in ["募資", "融資", "series", "million", "億", "獨角獸", "unicorn", "ipo", "上市"]):
            notable.append({"title": title[:80], "region": region, "url": r.get("url", "")})

    return {
        "total": len(rows),
        "region_count": region_count,
        "source_count": source_count,
        "industry_count": industry_count,
        "stage_count": stage_count,
        "processed_count": processed_count,
        "funding_articles": funding_articles[:10],
        "notable": notable[:8],
        "hotai_top": [],  # populated separately via load_hotai_top()
    }


# ── Rich terminal renderer ──

def render_terminal(tab_name: str, rows: list[dict], stats: dict):
    today = datetime.date.today()
    week_str = today.strftime("第 %V 週")
    date_str = today.strftime("%Y-%m-%d")

    # ── Header ──
    console.print()
    header = Text(justify="center")
    header.append("🚀  新 創 情 報 周 報  🚀\n", style="bold bright_white on dark_blue")
    header.append(f"  {week_str}  ·  {date_str}  ·  資料來源: {tab_name}  ", style="bold grey82 on dark_blue")
    console.print(Panel(Align.center(header), style="dark_blue", padding=(0, 2)))
    console.print()

    # ── Summary cards ──
    total = stats["total"]
    processed = stats["processed_count"].get("true", 0)
    funding_cnt = len(stats["funding_articles"])
    notable_cnt = len(stats["notable"])

    cards = [
        Panel(f"[bold bright_cyan]{total}[/]\n[grey50]篇文章抓取", title="總文章數", border_style="bright_cyan", padding=(0, 2)),
        Panel(f"[bold bright_green]{processed}[/]\n[grey50]篇 AI 處理完", title="已分析", border_style="bright_green", padding=(0, 2)),
        Panel(f"[bold bright_yellow]{funding_cnt}[/]\n[grey50]篇含融資資訊", title="融資新聞", border_style="bright_yellow", padding=(0, 2)),
        Panel(f"[bold bright_magenta]{notable_cnt}[/]\n[grey50]篇重點新聞", title="值得關注", border_style="bright_magenta", padding=(0, 2)),
    ]
    console.print(Columns(cards, equal=True, expand=True))
    console.print()

    # ── Region breakdown ──
    console.print(Rule("[bold bright_white]地區分佈", style="bright_blue"))
    console.print()
    region_table = Table(box=box.ROUNDED, show_header=True, header_style="bold bright_white", border_style="grey37", expand=True)
    region_table.add_column("地區", style="bold", min_width=10)
    region_table.add_column("文章數", justify="center", min_width=8)
    region_table.add_column("佔比", justify="center", min_width=12)
    region_table.add_column("文章量", min_width=30)

    for region in ["台灣", "中國", "東南亞", "全球"]:
        cnt = stats["region_count"].get(region, 0)
        pct = cnt / total * 100 if total else 0
        bar_len = int(pct / 3)
        bar = "█" * bar_len + "░" * (33 - bar_len)
        emoji = REGION_EMOJI.get(region, "")
        region_table.add_row(
            f"{emoji} {region}", str(cnt), f"{pct:.1f}%",
            f"[bright_blue]{bar}[/] [grey50]{pct:.0f}%[/]"
        )
    console.print(region_table)
    console.print()

    # ── Industry heatmap ──
    console.print(Rule("[bold bright_white]產業熱度", style="bright_blue"))
    console.print()
    top_industries = stats["industry_count"].most_common(10)
    max_ind = top_industries[0][1] if top_industries else 1

    ind_table = Table(box=box.SIMPLE, show_header=False, expand=True)
    ind_table.add_column("產業", min_width=14)
    ind_table.add_column("熱度條", min_width=45)
    ind_table.add_column("數量", justify="right", min_width=6)

    for ind, cnt in top_industries:
        color = INDUSTRY_COLOR.get(ind, "grey50")
        bar_len = int(cnt / max_ind * 40)
        bar = "▓" * bar_len + "░" * (40 - bar_len)
        ind_table.add_row(
            f"[{color}]● {ind}[/]",
            f"[{color}]{bar}[/]",
            f"[bold {color}]{cnt}[/]"
        )
    console.print(ind_table)
    console.print()

    # ── Stage distribution ──
    if stats["stage_count"]:
        console.print(Rule("[bold bright_white]融資輪次分佈", style="bright_blue"))
        console.print()
        stage_table = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold grey82", border_style="grey37")
        stage_table.add_column("輪次", min_width=12)
        for stage in STAGE_ORDER + ["IPO"]:
            if stage in stats["stage_count"]:
                stage_table.add_column(stage, justify="center", min_width=8)

        row_data = []
        for stage in STAGE_ORDER + ["IPO"]:
            cnt = stats["stage_count"].get(stage, None)
            if cnt is not None:
                row_data.append(f"[bold bright_green]{cnt}[/]")
        if row_data:
            stage_table.add_row("[bold]本週文章數[/]", *row_data)
        console.print(stage_table)
        console.print()

    # ── Notable funding news ──
    if stats["funding_articles"]:
        console.print(Rule("[bold bright_white]💰 融資亮點", style="bright_blue"))
        console.print()
        fund_table = Table(box=box.MINIMAL_DOUBLE_HEAD, show_header=True,
                           header_style="bold bright_yellow", border_style="grey37", expand=True)
        fund_table.add_column("#", justify="right", min_width=3, style="grey50")
        fund_table.add_column("公司 / 標題", min_width=50)
        fund_table.add_column("金額", justify="center", min_width=14, style="bright_green")
        fund_table.add_column("地區", justify="center", min_width=8)
        fund_table.add_column("輪次", justify="center", min_width=8)

        for i, item in enumerate(stats["funding_articles"], 1):
            emoji = REGION_EMOJI.get(item["region"], "")
            stage_text = f"[bright_cyan]{item['stage']}[/]" if item["stage"] else "[grey50]-[/]"
            fund_table.add_row(
                str(i),
                f"[bright_white]{item['title']}[/]",
                f"[bold bright_green]{item['funding']}[/]",
                f"{emoji} {item['region']}",
                stage_text,
            )
        console.print(fund_table)
        console.print()

    # ── Top sources ──
    console.print(Rule("[bold bright_white]📰 來源活躍度", style="bright_blue"))
    console.print()
    src_table = Table(box=box.SIMPLE, show_header=False, expand=True)
    src_table.add_column("來源", min_width=20)
    src_table.add_column("數量", justify="right", min_width=6)
    top_sources = stats["source_count"].most_common(8)
    max_src = top_sources[0][1] if top_sources else 1
    for src, cnt in top_sources:
        bar_len = int(cnt / max_src * 20)
        bar = "▪" * bar_len
        src_table.add_row(f"[bright_white]{src}[/]  [grey50]{bar}[/]", f"[bold]{cnt}[/]")
    console.print(src_table)
    console.print()

    # ── Hotai fit ranking (from Firebase) ──
    if stats.get("hotai_top"):
        console.print(Rule("[bold bright_white]和泰適配度排行 (Firebase AI 評分)", style="bright_blue"))
        console.print()
        hotai_table = Table(box=box.ROUNDED, show_header=True, header_style="bold bright_white",
                            border_style="grey37", expand=True)
        hotai_table.add_column("#",           justify="right", min_width=3)
        hotai_table.add_column("公司",         min_width=16)
        hotai_table.add_column("產業",         min_width=16)
        hotai_table.add_column("輪次",         min_width=8)
        hotai_table.add_column("集團適配度",   justify="center", min_width=10)
        hotai_table.add_column("新創推薦度",   justify="center", min_width=10)
        hotai_table.add_column("關鍵字",      justify="center", min_width=7)
        hotai_table.add_column("地區",         min_width=8)
        for i, doc in enumerate(stats["hotai_top"], 1):
            name     = doc.get("companyName") or doc.get("companyNameEn") or "—"
            industry = ", ".join((doc.get("industry") or [])[:2]) or "—"
            stage    = doc.get("stage") or "—"
            # 新欄位優先，fallback 舊欄位
            hotai    = doc.get("groupFitScore") or doc.get("hotaiFitScore")
            fit      = doc.get("startupScore")  or doc.get("fitScore")
            ml       = doc.get("mlScore")
            region   = doc.get("region", "—")
            def score_color(v):
                if v is None: return "grey50"
                return "bright_green" if v >= 7 else ("bright_yellow" if v >= 4 else "bright_red")
            hotai_table.add_row(
                str(i),
                f"[bold bright_white]{name[:18]}[/]",
                f"[grey70]{industry[:18]}[/]",
                f"[bright_cyan]{stage}[/]",
                f"[bold {score_color(hotai)}]{hotai:.1f}[/]" if hotai is not None else "—",
                f"[{score_color(fit)}]{fit:.1f}[/]"          if fit   is not None else "—",
                f"[grey70]{ml:.1f}[/]"                        if ml    is not None else "—",
                f"{REGION_EMOJI.get(region,'')} {region}",
            )
        console.print(hotai_table)
        console.print()

    # ── Notable articles ──
    if stats["notable"]:
        console.print(Rule("[bold bright_white]本週重點新聞", style="bright_blue"))
        console.print()
        for i, item in enumerate(stats["notable"], 1):
            emoji = REGION_EMOJI.get(item["region"], "🌐")
            console.print(f"  [grey50]{i:2d}.[/] {emoji} [bright_white]{item['title']}[/]")
            if item["url"]:
                console.print(f"       [link={item['url']}][grey50]{item['url'][:80]}[/][/]")
        console.print()

    # ── Footer ──
    console.print(Rule(style="grey37"))
    console.print(Align.center(
        f"[grey50]報告產生時間: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ·  "
        f"資料筆數: {total}  ·  Powered by Qwen 2.5 + Claude[/]"
    ))
    console.print()


# ── HTML renderer ──

_HTML_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #ffffff; color: #1a1a2e; font-family: 'Microsoft JhengHei', 'Noto Sans TC', Arial, sans-serif; font-size: 13px; }
.page { padding: 28px 32px; background: #ffffff; }
.region-page { page-break-before: always; padding: 28px 32px; }
/* Header */
.report-header { background: #1a3a6e; color: #ffffff; padding: 20px 24px; border-radius: 6px; margin-bottom: 20px; }
.report-header h1 { font-size: 1.4rem; font-weight: 800; color: #ffffff; margin-bottom: 4px; }
.report-header .meta { color: #a8c4f0; font-size: 0.82rem; }
.region-header { background: #1a3a6e; color: #ffffff; padding: 14px 20px; border-radius: 6px; margin-bottom: 16px; }
.region-header h2 { font-size: 1.1rem; font-weight: 700; color: #ffffff; }
.region-header .sub { color: #a8c4f0; font-size: 0.8rem; margin-top: 2px; }
/* Stat cards */
.cards-table { width: 100%; border-collapse: collapse; margin-bottom: 20px; border: 1px solid #dde3ee; mso-table-lspace: 0pt; mso-table-rspace: 0pt; }
.cards-table td { padding: 14px 0; text-align: center; border-right: 1px solid #dde3ee; background: #f0f4fc; }
.cards-table td:last-child { border-right: none; }
.card-num { font-size: 1.8rem; font-weight: 800; }
.card-label { color: #5a6a8a; font-size: 0.75rem; margin-top: 2px; }
/* Section */
.section { margin-bottom: 20px; }
.section-title { font-size: 0.85rem; font-weight: 700; color: #1a3a6e; margin-bottom: 10px;
  padding-bottom: 6px; border-bottom: 2px solid #1a3a6e; text-transform: uppercase; letter-spacing: .06em; }
/* Tables */
table.dt { width: 100%; border-collapse: collapse; font-size: 0.82rem; mso-table-lspace: 0pt; mso-table-rspace: 0pt; }
table.dt th { background: #eef2fb; color: #3a4a6a; padding: 7px 10px; text-align: left;
  font-weight: 700; font-size: 0.75rem; border-bottom: 2px solid #c8d4ec; }
table.dt td { padding: 7px 10px; border-bottom: 1px solid #eef0f6; vertical-align: top; }
table.dt tr:last-child td { border-bottom: none; }
/* Score badges */
.score-hi  { display: inline-block; background: #fff0dc; color: #a05000; font-weight: 700;
  padding: 2px 8px; border-radius: 4px; font-size: 0.82rem; border: 1px solid #f5c67a; }
.score-mid { display: inline-block; background: #ddeeff; color: #1a4a8e; font-weight: 700;
  padding: 2px 8px; border-radius: 4px; font-size: 0.82rem; border: 1px solid #9bc0f0; }
.score-lo  { display: inline-block; color: #8a9ab5; font-size: 0.82rem; }
.score-na  { color: #b0bac8; font-size: 0.82rem; }
/* Badges */
.badge { display: inline-block; padding: 2px 6px; border-radius: 3px; font-size: 0.72rem; font-weight: 700; }
.badge-green  { background: #d4f4e2; color: #1a7a3e; }
.badge-blue   { background: #ddeeff; color: #1a4a8e; }
.badge-orange { background: #fdebd0; color: #a05000; }
.badge-tag    { background: #eef3fb; color: #1a5cb5; border: 1px solid #b8d0f0; margin: 1px 2px 1px 0; }
/* Notable list */
.notable-list { list-style: none; }
.notable-list li { padding: 7px 0; border-bottom: 1px solid #eef0f6; }
.notable-list li:last-child { border-bottom: none; }
.notable-list a { color: #1a5cb5; text-decoration: underline; }
.idx { color: #8a9ab5; font-size: 0.78rem; }
/* Footer */
.footer { text-align: center; color: #8a9ab5; font-size: 0.75rem; margin-top: 20px;
  padding-top: 12px; border-top: 1px solid #dde3ee; }
/* Hotai callout box */
.hotai-note { background: #fff8ee; border-left: 4px solid #e07b00; padding: 8px 12px;
  margin-bottom: 12px; font-size: 0.78rem; color: #7a4800; border-radius: 0 4px 4px 0; }
a { color: #1a5cb5; }
@media print { body { background: #ffffff; } .region-page { page-break-before: always; } }
"""


def _score_badge(v: float | None) -> str:
    if v is None:
        return "<span class='score-na'>-</span>"
    if v >= 7:
        return f"<span class='score-hi'>{v:.1f}</span>"
    if v >= 4:
        return f"<span class='score-mid'>{v:.1f}</span>"
    return f"<span class='score-lo'>{v:.1f}</span>"


def _tags_html(doc: dict) -> str:
    """把 fitTags（FIT_KEYWORDS 分類 key）轉成和泰業務版圖的中文顯示名稱徽章。"""
    labels = [TAG_LABELS.get(t, t) for t in (doc.get("fitTags") or [])]
    if not labels:
        return "<span class='score-na'>-</span>"
    return "".join(f"<span class='badge badge-tag'>{_html.escape(l)}</span>" for l in labels)


def _bar_table(pct: float, total_width: int = 120) -> str:
    fill_w  = max(1, min(int(pct * total_width / 100), total_width - 1))
    empty_w = total_width - fill_w
    return (
        f"<table width='{total_width}' cellspacing='0' cellpadding='0' border='0' "
        f"style='height:6px;border-collapse:collapse;mso-table-lspace:0pt;mso-table-rspace:0pt'><tr>"
        f"<td width='{fill_w}' bgcolor='#2d7dd2' height='6' "
        f"style='height:6px;line-height:6px;font-size:1px;background:#2d7dd2'>&nbsp;</td>"
        f"<td width='{empty_w}' bgcolor='#dde3ee' height='6' "
        f"style='height:6px;line-height:6px;font-size:1px;background:#dde3ee'>&nbsp;</td>"
        f"</tr></table>"
    )


def _make_summary_page(tab_name: str, stats: dict, hotai_docs: list[dict],
                       today: datetime.date) -> str:
    week_str = today.strftime("第 %V 週")
    total    = stats["total"]
    max_ind  = max(stats["industry_count"].values(), default=1)
    max_src  = max(stats["source_count"].values(), default=1)

    # Region breakdown
    region_rows = ""
    for idx_r, region in enumerate(REGION_ORDER):
        cnt = stats["region_count"].get(region, 0)
        pct = cnt / total * 100 if total else 0
        row_bg = " bgcolor='#f8faff'" if idx_r % 2 == 1 else ""
        region_rows += (
            f"<tr{row_bg}><td>{region}</td><td><strong>{cnt}</strong></td><td>{pct:.1f}%</td>"
            f"<td>{_bar_table(pct)}</td></tr>"
        )

    # Industry heatmap
    industry_rows = ""
    for idx_i, (ind, cnt) in enumerate(stats["industry_count"].most_common(10)):
        pct = cnt / max_ind * 100
        row_bg = " bgcolor='#f8faff'" if idx_i % 2 == 1 else ""
        industry_rows += (
            f"<tr{row_bg}><td>{_html.escape(ind)}</td>"
            f"<td>{_bar_table(pct)}</td>"
            f"<td><strong>{cnt}</strong></td></tr>"
        )

    # Hotai top 10 ranking
    hotai_ranking = ""
    if hotai_docs:
        rows_h = ""
        for i, doc in enumerate(hotai_docs, 1):
            name    = _html.escape(doc.get("companyName") or doc.get("companyNameEn") or "—")
            name_en = _html.escape(doc.get("companyNameEn") or "")
            ind_str = _html.escape(", ".join((doc.get("industry") or [])[:2]) or "—")
            stage   = _html.escape(doc.get("stage") or "—")
            url     = _html.escape(doc.get("sourceUrl") or "")
            region  = _html.escape(doc.get("region") or "—")
            tags    = _tags_html(doc)
            # 新欄位優先，fallback 舊欄位（向後相容舊 Firebase 文件）
            hotai   = doc.get("groupFitScore") or doc.get("hotaiFitScore")
            fit     = doc.get("startupScore")  or doc.get("fitScore")
            ml      = doc.get("mlScore")
            name_link = f"<a href='{url}' target='_blank'>{name}</a>" if url else name
            name_cell = name_link + (f"<br><small style='color:#7a8aaa'>{name_en}</small>" if name_en else "")
            row_bg = " bgcolor='#f8faff'" if i % 2 == 0 else ""
            rows_h += (
                f"<tr{row_bg}><td class='idx'>{i}</td>"
                f"<td>{name_cell}</td>"
                f"<td><span class='badge badge-blue'>{ind_str}</span></td>"
                f"<td><span class='badge badge-orange'>{stage}</span></td>"
                f"<td>{_score_badge(hotai)}</td>"
                f"<td>{_score_badge(fit)}</td>"
                f"<td style='color:#7a8aaa'>{'%.1f' % ml if ml is not None else '-'}</td>"
                f"<td>{region}</td>"
                f"<td><small style='color:#7a8aaa'>{tags}</small></td></tr>"
            )
        hotai_ranking = f"""
<div class='section'>
  <div class='section-title'>和泰集團適配度排行 Top 10</div>
  <div class='hotai-note'>
    <strong>集團適配度</strong>：2.5基準分 + 40% Qwen語意 + 40% 業務關鍵字 + 20% 地區/輪次規則 &nbsp;｜&nbsp;
    <strong>新創推薦度</strong>：40% Qwen新聞可信度 + 25% 融資金額 + 20% 輪次成熟度 + 15% 投資人/描述品質
  </div>
  <table class='dt' cellspacing='0'><thead><tr>
    <th>#</th><th>公司</th><th>產業</th><th>輪次</th>
    <th>集團適配度</th><th>新創推薦度</th><th>關鍵字</th><th>地區</th><th>業務標籤</th>
  </tr></thead><tbody>{rows_h}</tbody></table>
</div>"""

    # Source activity
    source_rows = ""
    for idx_s, (src, cnt) in enumerate(stats["source_count"].most_common(10)):
        pct = cnt / max_src * 100
        row_bg = " bgcolor='#f8faff'" if idx_s % 2 == 1 else ""
        source_rows += (
            f"<tr{row_bg}><td>{_html.escape(src)}</td>"
            f"<td>{_bar_table(pct)}</td>"
            f"<td><strong>{cnt}</strong></td></tr>"
        )

    return f"""<div class='page'>
  <div class='report-header'>
    <h1>新創情報周報</h1>
    <div class='meta'>{week_str} &nbsp;·&nbsp; {today.strftime('%Y-%m-%d')} &nbsp;·&nbsp; {_html.escape(tab_name)}</div>
    <div class='meta'>🔗 <a href='https://startup-claw-ht-ochre.vercel.app/'>查看完整儀表板 (startup-claw-ht-ochre.vercel.app)</a></div>
  </div>

  <table class='cards-table' cellspacing='0' cellpadding='0'><tr>
    <td bgcolor='#f0f4fc'><div class='card-num' style='color:#1a5cb5'>{total}</div><div class='card-label'>總文章數</div></td>
    <td bgcolor='#f0f4fc'><div class='card-num' style='color:#1a7a3e'>{stats['processed_count'].get('true',0)}</div><div class='card-label'>AI 已分析</div></td>
    <td bgcolor='#f0f4fc'><div class='card-num' style='color:#a05000'>{len(stats['funding_articles'])}</div><div class='card-label'>融資新聞</div></td>
    <td bgcolor='#f0f4fc'><div class='card-num' style='color:#6a1a8e'>{len(stats['notable'])}</div><div class='card-label'>重點新聞</div></td>
  </tr></table>

  <div class='section'>
    <div class='section-title'>地區分佈</div>
    <table class='dt' cellspacing='0'><thead><tr><th>地區</th><th>文章數</th><th>佔比</th><th>趨勢</th></tr></thead>
    <tbody>{region_rows}</tbody></table>
  </div>

  <div class='section'>
    <div class='section-title'>產業熱度 Top 10</div>
    <table class='dt' cellspacing='0'><thead><tr><th>產業</th><th>熱度</th><th>數量</th></tr></thead>
    <tbody>{industry_rows}</tbody></table>
  </div>

  {hotai_ranking}

  <div class='section'>
    <div class='section-title'>來源活躍度</div>
    <table class='dt' cellspacing='0'><thead><tr><th>媒體</th><th>趨勢</th><th>數量</th></tr></thead>
    <tbody>{source_rows}</tbody></table>
  </div>

  <div class='footer'>完整儀表板: <a href='https://startup-claw-ht-ochre.vercel.app/'>startup-claw-ht-ochre.vercel.app</a> &nbsp;·&nbsp; 報告產生時間: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} &nbsp;·&nbsp; Powered by Qwen 2.5 + Claude</div>
</div>"""




def _make_region_page(region: str, scored_map: dict) -> str:
    """Build a page from Firebase scored docs for this region only."""
    all_docs = [d for d in scored_map.values() if d.get("region") == region]

    # 東南亞：排除台灣/中國來源的文章
    if region == "東南亞":
        all_docs = [d for d in all_docs
                    if d.get("sourceId", "") not in SEA_EXCLUDE_SOURCES]

    group_fit   = lambda d: d.get("groupFitScore") or d.get("hotaiFitScore") or 0
    has_funding = lambda d: bool(d.get("stage") or d.get("fundingAmountRaw"))
    max_count   = REGION_DISPLAY_MAX.get(region, 20)
    min_count   = REGION_DISPLAY_MIN.get(region, 5)

    # 門檻過濾
    docs = [d for d in all_docs if group_fit(d) >= MIN_DISPLAY_GROUP_FIT]

    # 若過濾後低於下限，放寬門檻補到下限數量
    if len(docs) < min_count:
        extra = sorted(
            [d for d in all_docs if d not in docs],
            key=group_fit, reverse=True
        )
        docs = docs + extra[:max(0, min_count - len(docs))]

    # 排序：集團適配度降序為主，有融資的在同分時優先；最多取上限
    docs = sorted(docs, key=lambda d: (group_fit(d), has_funding(d)), reverse=True)[:max_count]

    filtered_out = len([d for d in scored_map.values()
                        if d.get("region") == region]) - len(docs)

    if not docs:
        reason = "本週尚無已評分文章（請先執行 python main.py 完整流程）"
        return (f"<div class='region-page'>"
                f"<div class='region-header'><h2>{_html.escape(region)} 新創投資情報</h2>"
                f"<div class='sub'>{reason}</div></div></div>")

    rows_html = ""
    for i, doc in enumerate(docs, 1):
        url       = _html.escape(doc.get("sourceUrl") or "")
        # ── 新聞標題：優先用原始新聞標題，舊資料（無 newsTitle）fallback 到公司名 ──
        _raw_title = doc.get("newsTitle") or ""
        _has_real_title = bool(_raw_title and _raw_title != doc.get("companyName", ""))
        news_title = _html.escape(
            (_raw_title or doc.get("companyName") or doc.get("companyNameEn") or "—")[:120]
        )
        company   = _html.escape(doc.get("companyName") or doc.get("companyNameEn") or "—")
        name_en   = _html.escape(doc.get("companyNameEn") or "")
        industry  = _html.escape(", ".join((doc.get("industry") or [])[:2]) or "—")
        stage     = _html.escape(doc.get("stage") or "—")
        # ── AI 生成摘要（優先 description，再 summary）──
        ai_summary = _html.escape((doc.get("description") or doc.get("summary") or "")[:200])
        funding   = _html.escape(doc.get("fundingAmountRaw") or "")
        extracted = (doc.get("extractedAt") or "")[:10]
        hotai     = doc.get("hotaiFitScore")
        fit       = doc.get("fitScore")
        ml        = doc.get("mlScore")
        tags_cell = _tags_html(doc)

        # 新聞標題欄：有真實標題用藍色粗體，舊資料（公司名 fallback）用灰色斜體
        if _has_real_title:
            title_style = "color:#1a5cb5;font-weight:600"
            title_display = news_title
        else:
            title_style = "color:#8a9ab5;font-style:italic"
            title_display = news_title + " ⟨待更新⟩"
        title_cell = (
            f"<a href='{url}' target='_blank' style='{title_style}'>{title_display}</a>"
            if url else f"<span style='{title_style}'>{title_display}</span>"
        )

        # 公司名稱欄
        company_cell = f"<strong>{company}</strong>"
        if name_en and name_en != company:
            company_cell += f"<br><small style='color:#7a8aaa'>{name_en}</small>"
        stage_html   = f"<br><span class='badge badge-orange'>{stage}</span>" if stage != "—" else ""
        funding_html = f" <span class='badge badge-green'>{funding}</span>" if funding else ""
        company_cell += stage_html + funding_html

        row_bg = " bgcolor='#f8faff'" if i % 2 == 0 else ""
        rows_html += (
            f"<tr{row_bg}>"
            f"<td class='idx'>{i}</td>"
            f"<td>{title_cell}</td>"                                          # 新聞標題
            f"<td><span class='badge badge-blue'>{industry}</span><br>{company_cell}</td>"  # 公司名稱
            f"<td style='color:#4a5a7a;font-size:.81rem'>{ai_summary}</td>"  # AI 生成摘要
            f"<td>{tags_cell}</td>"                                           # 和泰相關事業
            f"<td style='color:#8a9ab5;white-space:nowrap;font-size:.78rem'>{extracted}</td>"
            f"<td style='text-align:center'>{_score_badge(hotai)}</td>"
            f"<td style='text-align:center'>{_score_badge(fit)}</td>"
            f"<td style='text-align:center;color:#8a9ab5'>{'%.1f' % ml if ml is not None else '-'}</td>"
            f"</tr>"
        )

    return f"""<div class='region-page'>
  <div class='region-header'>
    <h2>{_html.escape(region)} 新創投資情報</h2>
    <div class='sub'>
      顯示 {len(docs)} 家（集團適配度 ≥ {MIN_DISPLAY_GROUP_FIT}，依集團適配度由高到低排序）
      {"&nbsp;·&nbsp; 已過濾 " + str(filtered_out) + " 筆低相關" if filtered_out else ""}
    </div>
  </div>
  <div class='hotai-note'>集團適配度：2.5基準分 + 40% Qwen語意 + 40% 業務關鍵字 + 20% 地區/輪次規則</div>
  <table class='dt' cellspacing='0'>
    <thead><tr>
      <th>#</th>
      <th>新聞標題</th>
      <th>相關公司名稱 / 產業 / 輪次</th>
      <th>AI 生成摘要</th>
      <th>和泰相關事業</th>
      <th>評分日期</th>
      <th style='text-align:center'>集團適配度</th>
      <th style='text-align:center'>新創推薦度</th>
      <th style='text-align:center'>關鍵字</th>
    </tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
</div>"""


def _make_scoring_legend() -> str:
    """在 HTML 報告最後加上三個評分的計算基準說明。"""
    from config import FIT_KEYWORDS

    # 12 大業務版圖 → 中文對應
    vertical_names = {
        "AutoRetail":        "① 汽車代理經銷（Toyota/Lexus）",
        "CommercialVehicle": "② ③ 商用車（HINO 卡車/巴士）",
        "EV_Charging":       "④ 充電/能源（EVRun/氫能）",
        "AutoFinance":       "⑤ 金融（和潤企業/車貸）",
        "CarRental_Fleet":   "⑥ 租車/車隊（和運/iRent）",
        "AutoProduct":       "⑧ 車用產品（車美仕/ADAS）",
        "VehicleBody":       "⑨ 車體製造（和泰巴士）",
        "InsurTech":         "⑩ 產險科技（和泰產險）",
        "IndustrialRobot":   "⑪ 倉儲機器人（TMHT叉車/AGV）",
        "MaaS_Mobility":     "⑫ MaaS（yoxi/去趣/和泰聯網）",
        "HVAC_Energy":       "⑬ 空調（大金 Daikin）",
        "AI_DataPlatform":   "AI 數位平台（橫跨各業務）",
    }

    kw_rows = ""
    for cat, kws in FIT_KEYWORDS.items():
        label = vertical_names.get(cat, cat)
        kw_sample = " &nbsp;·&nbsp; ".join(_html.escape(k) for k in kws[:6])
        if len(kws) > 6:
            kw_sample += f" &nbsp;·&nbsp; <em>+{len(kws)-6} 更多</em>"
        kw_rows += (
            f"<tr><td style='white-space:nowrap;font-weight:600;color:#1a3a6e'>{label}</td>"
            f"<td style='color:#4a5a7a;font-size:.8rem'>{kw_sample}</td></tr>"
        )

    return f"""<div class='page' style='margin-top:20px'>
  <div class='region-header'>
    <h2>📊 評分基準說明</h2>
    <div class='sub'>三個維度的定義與計算方式</div>
  </div>

  <div class='section'>
    <div class='section-title'>評分維度定義</div>
    <table class='dt' cellspacing='0'>
      <thead><tr>
        <th style='width:15%'>評分欄位</th>
        <th style='width:30%'>定義</th>
        <th style='width:30%'>計算方式</th>
        <th style='width:25%'>分數區間說明</th>
      </tr></thead>
      <tbody>
        <tr>
          <td><strong>集團適配度</strong><br><small style='color:#7a8aaa'>groupFitScore</small></td>
          <td>與和泰集團 13 大業務版圖的策略契合程度</td>
          <td>
            <span class='badge badge-orange'>40%</span> Qwen 大模型語意評分（hotaiFitScore）<br>
            <span class='badge badge-blue'>40%</span> 業務關鍵字 ML 覆蓋率（mlScore）<br>
            <span class='badge badge-green'>20%</span> 地區 / 輪次商業規則加分
          </td>
          <td>
            <span class='score-hi'>7–10</span> 高度契合，建議優先關注<br>
            <span class='score-mid'>4–6.9</span> 中度相關，可觀察<br>
            <span class='score-lo'>0–3.9</span> 低相關
          </td>
        </tr>
        <tr>
          <td><strong>新創推薦度</strong><br><small style='color:#7a8aaa'>startupScore</small></td>
          <td>新創公司本身的投資價值與新聞可信度</td>
          <td>
            <span class='badge badge-orange'>40%</span> Qwen 新聞完整性（relevanceScore）<br>
            <span class='badge badge-blue'>25%</span> 融資金額規模<br>
            <span class='badge badge-green'>20%</span> 輪次成熟度<br>
            <span class='badge badge-orange'>15%</span> 投資人資訊品質
          </td>
          <td>
            <span class='score-hi'>7–10</span> 具體融資輪次 + 金額 + 知名投資人<br>
            <span class='score-mid'>4–6.9</span> 部分資訊確認<br>
            <span class='score-lo'>0–3.9</span> 資訊不完整
          </td>
        </tr>
        <tr>
          <td><strong>關鍵字分數</strong><br><small style='color:#7a8aaa'>mlScore</small></td>
          <td>純機器學習關鍵字命中率，不依賴 AI 判斷</td>
          <td>
            掃描 12 大業務版圖的 FIT_KEYWORDS 關鍵字<br>
            每個類別命中 → 加分，上限 10 分<br>
            <em>公式：min(命中類別加總 / 2 × 10, 10)，命中滿 2 個類別即滿分</em>
          </td>
          <td>
            可用來驗證 Qwen 評分是否合理<br>
            若 mlScore 高但 groupFitScore 低 → 可能有誤判
          </td>
        </tr>
      </tbody>
    </table>
  </div>

  <div class='section' style='font-size:.8rem;color:#4a5a7a'>
    <div class='section-title'>AI 怎麼打分（relevanceScore / hotaiFitScore）</div>
    <p style='margin-bottom:6px'>
      Qwen／Gemini 不是自由發揮，是照給定的規則打分：prompt 裡會附上 0–10 分的錨點說明
      （例如「9–10 分 = 有明確金額與投資人的確定融資消息」「0–2 分 = 總經/股市/政策等不相關內容」），
      並整段附上和泰集團 13 大業務版圖與 2026 策略重點的介紹，讓模型知道「契合」具體是在比對什麼。
    </p>
    <p>
      這兩個 AI 自評分數各自只佔最終分數的 <strong>40% 權重</strong>，
      會再跟關鍵字命中（mlScore）、地區/輪次業務規則加分混合計算，不是唯一依據。
    </p>
  </div>

  <div class='section'>
    <div class='section-title'>12 大業務版圖關鍵字清單（FIT_KEYWORDS）</div>
    <p style='font-size:.8rem;color:#7a8aaa;margin-bottom:8px'>
      以下關鍵字用於計算 <strong>關鍵字分數（mlScore）</strong>，
      也作為 <strong>集團適配度（groupFitScore）</strong> 的 40% ML 分數依據。
    </p>
    <table class='dt' cellspacing='0'>
      <thead><tr><th>業務版圖</th><th>關鍵字（每類取前 6 個顯示）</th></tr></thead>
      <tbody>{kw_rows}</tbody>
    </table>
  </div>

  <div class='section' style='font-size:.78rem;color:#7a8aaa;border-top:1px solid #eef0f6;padding-top:12px'>
    <strong>摘要語言規則：</strong>
    台灣・中國地區的新聞摘要以繁體中文生成；東南亞・全球地區以英文生成。<br>
    <strong>顯示門檻：</strong>集團適配度 ≥ {MIN_DISPLAY_GROUP_FIT} 才顯示於各地區報告中。
    顯示數量上限：台灣 {REGION_DISPLAY_MAX['台灣']}・中國 {REGION_DISPLAY_MAX['中國']}・
    東南亞 {REGION_DISPLAY_MAX['東南亞']}・全球 {REGION_DISPLAY_MAX['全球']}。<br>
    <strong>LLM：</strong>Gemini 2.5 Flash-Lite（主要）+ Qwen3 8B（本地 Ollama，額度用完時備援）&nbsp;·&nbsp;
    <strong>報告產生：</strong>{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}
  </div>
</div>"""


def render_html(tab_name: str, rows: list[dict], stats: dict,
                scored_map: dict | None = None) -> str:
    if scored_map is None:
        scored_map = {}

    today = datetime.date.today()

    # Hotai top 10 from scored_map (all scored docs, sorted — no Firebase query limit)
    hotai_docs = sorted(
        [d for d in scored_map.values() if d.get("hotaiFitScore") is not None],
        key=lambda d: d["hotaiFitScore"], reverse=True,
    )[:10]

    summary = _make_summary_page(tab_name, stats, hotai_docs, today)
    pages   = [summary]
    for region in REGION_ORDER:
        pages.append(_make_region_page(region, scored_map))
    pages.append(_make_scoring_legend())

    body = "\n".join(pages)
    return f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>新創情報周報 {today.strftime("第%V週")}</title>
<!--[if !mso]><!-->
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+TC:wght@400;700;800&display=swap" rel="stylesheet">
<!--<![endif]-->
<!--[if mso]><style>body,td,th,a{{font-family:'Microsoft JhengHei',Arial,sans-serif!important;}}</style><![endif]-->
<style>{_HTML_CSS}</style>
</head>
<body>
{body}
</body>
</html>"""


# ── Entry point ──

def main():
    args = sys.argv[1:]
    output_html  = "--html"  in args
    send_email   = "--email" in args
    dry_email    = "--dry-email" in args
    args = [a for a in args if not a.startswith("--")]

    if args:
        tab_name = args[0]
        console.print(f"[grey50]載入 tab: {tab_name}[/]")
        rows = load_single_tab(tab_name)
    else:
        tab_name = "raw_" + datetime.date.today().strftime("%Y-%m-%d")
        console.print("[grey50]載入本週所有 raw_* tabs...[/]")
        try:
            rows = load_all_tabs()
            if not rows:
                rows = load_single_tab(tab_name)
        except Exception:
            rows = load_single_tab(tab_name)

    if not rows:
        console.print("[red]沒有資料，請先跑 python main.py[/]")
        return

    stats = analyze_rows(rows)
    from ai_processor import collection_for_tab
    scored_map = load_scored_map(collection_for_tab(tab_name))
    # pass to terminal renderer for Hotai ranking table
    stats["hotai_top"] = sorted(
        [d for d in scored_map.values() if d.get("hotaiFitScore") is not None],
        key=lambda d: d["hotaiFitScore"], reverse=True,
    )[:10]
    render_terminal(tab_name, rows, stats)

    if output_html or send_email or dry_email:
        html = render_html(tab_name, rows, stats, scored_map=scored_map)

    if output_html:
        filename = f"weekly_report_{datetime.date.today()}.html"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(html)
        console.print(f"[bright_green]✅ HTML 報告已儲存: {filename}[/]")

    if send_email:
        from email_sender import send_weekly_report
        ok = send_weekly_report(html)
        if ok:
            console.print("[bright_green]✅ 周報已寄出[/]")
        else:
            console.print("[yellow]⚠️  Email 失敗，已存成本地 HTML 備份[/]")

    if dry_email:
        from email_sender import dry_run
        dry_run(html)
        console.print("[grey50][dry-run] Email 模擬完成[/]")


if __name__ == "__main__":
    main()
