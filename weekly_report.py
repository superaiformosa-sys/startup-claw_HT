"""
新創周報產生器
從 Google Sheets 讀取本週資料，產出終端機彩色報告 + HTML 檔案
Usage:
    python weekly_report.py                   # 用今天的 tab
    python weekly_report.py raw_2026-05-20    # 指定 tab
    python weekly_report.py --html            # 同時輸出 HTML
"""
import sys
import json
import re
import datetime
from collections import Counter, defaultdict
from ai_processor import get_sheet
from config import SOURCES

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.columns import Columns
from rich.text import Text
from rich.rule import Rule
from rich import box
from rich.align import Align
from rich.layout import Layout
from rich.padding import Padding

console = Console(width=110)

REGION_EMOJI = {"台灣": "🇹🇼", "中國": "🇨🇳", "東南亞": "🌏", "全球": "🌐"}
INDUSTRY_COLOR = {
    "AI": "bright_cyan", "SaaS": "bright_blue", "FinTech": "bright_green",
    "醫療": "bright_red", "物流": "yellow", "電商": "bright_magenta",
    "Mobility": "orange3", "InsurTech": "steel_blue1", "GreenTech": "green3",
    "EdTech": "gold1", "生技": "pale_turquoise1", "半導體": "bright_white",
    "機器人": "plum2", "CyberSecurity": "red1", "區塊鏈": "deep_sky_blue1",
    "硬體": "grey74", "其他": "grey50",
}
STAGE_ORDER = ["種子輪", "天使輪", "Pre-A", "A輪", "B輪", "C輪", "D輪", "戰略投資"]


# ── Data loading ──

def load_all_tabs(gc_client=None) -> list[dict]:
    """載入 Google Sheets 所有 raw_* tab 的資料"""
    import gspread
    from google.oauth2.service_account import Credentials
    from config import SHEETS_ID, GOOGLE_CREDENTIALS_JSON

    SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(json.loads(GOOGLE_CREDENTIALS_JSON), scopes=SCOPES)
    gc = gspread.authorize(creds)
    ss = gc.open_by_key(SHEETS_ID)

    today = datetime.date.today()
    week_ago = today - datetime.timedelta(days=7)
    rows = []

    for ws in ss.worksheets():
        if not ws.title.startswith("raw_"):
            continue
        try:
            date_str = ws.title.replace("raw_", "")
            tab_date = datetime.date.fromisoformat(date_str)
            if tab_date < week_ago:
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


# ── Article title analysis (no AI needed) ──

INDUSTRY_KEYWORDS = {
    "AI": ["ai", "人工智慧", "llm", "大模型", "機器學習", "深度學習", "gpt", "生成式"],
    "FinTech": ["支付", "fintech", "金融科技", "借貸", "區塊鏈", "數位銀行", "crypto", "defi"],
    "生技": ["生技", "biotech", "醫藥", "基因", "藥物", "臨床", "製藥", "生物"],
    "醫療": ["醫療", "健康", "medtech", "遠距", "診斷", "health", "醫院", "醫材"],
    "SaaS": ["saas", "雲端", "軟體", "erp", "crm", "b2b", "訂閱", "enterprise"],
    "電商": ["電商", "電子商務", "零售", "marketplace", "ecommerce", "購物"],
    "Mobility": ["自駕", "電動車", "ev", "充電", "共乘", "車聯網", "mobility"],
    "GreenTech": ["green", "永續", "碳", "solar", "再生能源", "cleantech", "淨零"],
    "EdTech": ["教育", "edtech", "學習", "課程", "teaching"],
    "半導體": ["晶片", "半導體", "chip", "wafer", "封裝", "ic設計"],
    "物流": ["物流", "供應鏈", "倉儲", "配送", "logistics"],
    "機器人": ["機器人", "robot", "automation", "自動化"],
}


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
        "種子輪": ["種子輪", "seed", "天使輪", "angel"],
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

    # ── Notable articles ──
    if stats["notable"]:
        console.print(Rule("[bold bright_white]⭐ 本週重點新聞", style="bright_blue"))
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

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>新創情報周報 {week}</title>
<style>
  :root {{
    --bg: #0d1117; --card: #161b22; --border: #30363d;
    --text: #c9d1d9; --muted: #8b949e; --accent: #58a6ff;
    --green: #3fb950; --yellow: #d29922; --red: #f85149;
    --purple: #bc8cff; --cyan: #79c0ff;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text); font-family: -apple-system, 'Segoe UI', sans-serif; padding: 24px; }}
  .container {{ max-width: 1100px; margin: 0 auto; }}
  .header {{ text-align: center; padding: 40px 0 30px; border-bottom: 1px solid var(--border); margin-bottom: 30px; }}
  .header h1 {{ font-size: 2.4rem; font-weight: 800; background: linear-gradient(135deg, #58a6ff, #bc8cff); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
  .header .meta {{ color: var(--muted); margin-top: 8px; font-size: 0.95rem; letter-spacing: 0.05em; }}
  .cards {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 30px; }}
  .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 20px; text-align: center; transition: transform 0.2s; }}
  .card:hover {{ transform: translateY(-2px); }}
  .card .num {{ font-size: 2.5rem; font-weight: 700; }}
  .card .label {{ color: var(--muted); font-size: 0.85rem; margin-top: 4px; }}
  .section {{ margin-bottom: 30px; }}
  .section-title {{ font-size: 1.1rem; font-weight: 700; color: var(--accent); margin-bottom: 16px; padding-bottom: 8px; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 8px; }}
  table {{ width: 100%; border-collapse: collapse; background: var(--card); border-radius: 10px; overflow: hidden; border: 1px solid var(--border); }}
  th {{ background: #1c2128; color: var(--muted); padding: 10px 16px; text-align: left; font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.05em; }}
  td {{ padding: 10px 16px; border-top: 1px solid var(--border); font-size: 0.92rem; vertical-align: top; }}
  tr:hover td {{ background: rgba(88, 166, 255, 0.05); }}
  .bar-wrap {{ background: #1c2128; border-radius: 4px; height: 8px; overflow: hidden; }}
  .bar-fill {{ height: 100%; border-radius: 4px; background: linear-gradient(90deg, #58a6ff, #bc8cff); }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 0.78rem; font-weight: 600; }}
  .badge-green {{ background: rgba(63,185,80,0.15); color: var(--green); }}
  .badge-blue {{ background: rgba(88,166,255,0.15); color: var(--cyan); }}
  .badge-yellow {{ background: rgba(210,153,34,0.15); color: var(--yellow); }}
  .badge-purple {{ background: rgba(188,140,255,0.15); color: var(--purple); }}
  .notable-list {{ list-style: none; }}
  .notable-list li {{ padding: 10px 16px; border-bottom: 1px solid var(--border); display: flex; align-items: flex-start; gap: 12px; }}
  .notable-list li:last-child {{ border-bottom: none; }}
  .notable-list a {{ color: var(--text); text-decoration: none; }}
  .notable-list a:hover {{ color: var(--accent); }}
  .idx {{ color: var(--muted); min-width: 24px; text-align: right; padding-top: 2px; font-size: 0.85rem; }}
  .footer {{ text-align: center; color: var(--muted); font-size: 0.82rem; padding: 24px 0; border-top: 1px solid var(--border); margin-top: 20px; }}
  @media (max-width: 700px) {{ .cards {{ grid-template-columns: repeat(2, 1fr); }} }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>🚀 新創情報周報</h1>
    <div class="meta">{week} &nbsp;·&nbsp; {date} &nbsp;·&nbsp; 資料來源: {tab}</div>
  </div>

  <div class="cards">
    <div class="card"><div class="num" style="color:#58a6ff">{total}</div><div class="label">總文章數</div></div>
    <div class="card"><div class="num" style="color:#3fb950">{processed}</div><div class="label">AI 已分析</div></div>
    <div class="card"><div class="num" style="color:#d29922">{funding_cnt}</div><div class="label">融資新聞</div></div>
    <div class="card"><div class="num" style="color:#bc8cff">{notable_cnt}</div><div class="label">重點新聞</div></div>
  </div>

  <div class="section">
    <div class="section-title">🌍 地區分佈</div>
    <table>
      <thead><tr><th>地區</th><th>文章數</th><th>佔比</th><th>文章量</th></tr></thead>
      <tbody>{region_rows}</tbody>
    </table>
  </div>

  <div class="section">
    <div class="section-title">🔥 產業熱度</div>
    <table>
      <thead><tr><th>產業</th><th>熱度</th><th>文章數</th></tr></thead>
      <tbody>{industry_rows}</tbody>
    </table>
  </div>

  {funding_section}

  <div class="section">
    <div class="section-title">⭐ 本週重點新聞</div>
    <div class="card" style="padding: 0;">
      <ul class="notable-list">{notable_items}</ul>
    </div>
  </div>

  <div class="section">
    <div class="section-title">📰 來源活躍度</div>
    <table>
      <thead><tr><th>媒體</th><th>文章數</th></tr></thead>
      <tbody>{source_rows}</tbody>
    </table>
  </div>

  <div class="footer">報告產生時間: {generated_at} &nbsp;·&nbsp; Powered by Qwen 2.5 + Claude</div>
</div>
</body>
</html>"""


def render_html(tab_name: str, rows: list[dict], stats: dict) -> str:
    today = datetime.date.today()
    week_str = today.strftime("第 %V 週")
    total = stats["total"]
    max_ind = max(stats["industry_count"].values(), default=1)
    max_src = max(stats["source_count"].values(), default=1)

    region_rows = ""
    for region in ["台灣", "中國", "東南亞", "全球"]:
        cnt = stats["region_count"].get(region, 0)
        pct = cnt / total * 100 if total else 0
        emoji = REGION_EMOJI.get(region, "")
        region_rows += (
            f"<tr><td>{emoji} {region}</td><td><strong>{cnt}</strong></td><td>{pct:.1f}%</td>"
            f"<td><div class='bar-wrap'><div class='bar-fill' style='width:{pct:.0f}%'></div></div></td></tr>"
        )

    industry_rows = ""
    for ind, cnt in stats["industry_count"].most_common(10):
        pct = cnt / max_ind * 100
        industry_rows += (
            f"<tr><td>{ind}</td>"
            f"<td><div class='bar-wrap'><div class='bar-fill' style='width:{pct:.0f}%'></div></div></td>"
            f"<td><strong>{cnt}</strong></td></tr>"
        )

    funding_section = ""
    if stats["funding_articles"]:
        rows_html = ""
        for i, item in enumerate(stats["funding_articles"], 1):
            emoji = REGION_EMOJI.get(item["region"], "")
            stage_badge = f"<span class='badge badge-blue'>{item['stage']}</span>" if item["stage"] else "-"
            rows_html += (
                f"<tr><td class='idx'>{i}</td>"
                f"<td><a href='{item['url']}' target='_blank'>{item['title']}</a></td>"
                f"<td><span class='badge badge-green'>{item['funding']}</span></td>"
                f"<td>{emoji} {item['region']}</td>"
                f"<td>{stage_badge}</td></tr>"
            )
        funding_section = (
            "<div class='section'><div class='section-title'>💰 融資亮點</div>"
            "<table><thead><tr><th>#</th><th>標題</th><th>金額</th><th>地區</th><th>輪次</th></tr></thead>"
            f"<tbody>{rows_html}</tbody></table></div>"
        )

    notable_items = ""
    for i, item in enumerate(stats["notable"], 1):
        emoji = REGION_EMOJI.get(item["region"], "🌐")
        link = f"<a href='{item['url']}' target='_blank'>{item['title']}</a>" if item["url"] else item["title"]
        notable_items += f"<li><span class='idx'>{i}</span><span>{emoji} {link}</span></li>"

    source_rows = ""
    for src, cnt in stats["source_count"].most_common(10):
        pct = cnt / max_src * 100
        source_rows += (
            f"<tr><td>{src}</td>"
            f"<td><div style='display:flex;align-items:center;gap:10px'>"
            f"<div class='bar-wrap' style='flex:1'><div class='bar-fill' style='width:{pct:.0f}%'></div></div>"
            f"<strong style='min-width:30px;text-align:right'>{cnt}</strong></div></td></tr>"
        )

    return HTML_TEMPLATE.format(
        week=week_str,
        date=today.strftime("%Y-%m-%d"),
        tab=tab_name,
        total=total,
        processed=stats["processed_count"].get("true", 0),
        funding_cnt=len(stats["funding_articles"]),
        notable_cnt=len(stats["notable"]),
        region_rows=region_rows,
        industry_rows=industry_rows,
        funding_section=funding_section,
        notable_items=notable_items,
        source_rows=source_rows,
        generated_at=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


# ── Entry point ──

def main():
    args = sys.argv[1:]
    output_html = "--html" in args
    args = [a for a in args if not a.startswith("--")]

    if args:
        tab_name = args[0]
        console.print(f"[grey50]載入 tab: {tab_name}[/]")
        rows = load_single_tab(tab_name)
    else:
        tab_name = "raw_" + datetime.date.today().strftime("%Y-%m-%d")
        console.print(f"[grey50]載入本週所有 raw_* tabs...[/]")
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
    render_terminal(tab_name, rows, stats)

    if output_html:
        html = render_html(tab_name, rows, stats)
        filename = f"weekly_report_{datetime.date.today()}.html"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(html)
        console.print(f"[bright_green]✅ HTML 報告已儲存: {filename}[/]")


if __name__ == "__main__":
    main()
