import json
import re
import time
import logging
from datetime import datetime, timezone
import requests
import gspread
from google.oauth2.service_account import Credentials
from config import (
    GEMINI_API_KEY, GEMINI_ENDPOINT, MAX_GEMINI_PER_RUN,
    SHEETS_ID, GOOGLE_CREDENTIALS_JSON, FIT_KEYWORDS, FX,
)
from firebase_client import firestore_write

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
TAIWAN_DOMAINS = ["meet.bnext", "bnext.com", "inside.com.tw", "technews.tw", "news.google.com", "ctee.com.tw", "udn.com"]

OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_MODEL = "qwen2.5:7b"

# ── 規則集 ──

# 標題出現任一關鍵字 → 直接跳過（不送 Qwen）
RULE_SKIP_TITLE = [
    # 股市
    "股市", "大盤", "成交額", "成交量", "漲跌", "休盤", "收盤", "開盤", "指數",
    "沪深", "恒指", "道指", "納指", "标普", "北向資金", "主力資金", "南向資金",
    "半日", "午間", "日线", # 盤中報
    # 總經/政策
    "央行", "降準", "降息", "升息", "關稅", "制裁", "外匯", "匯率", "期貨",
    "cpi", "ppi", "gdp", "通膨", "通胀", "貿易戰", "外貿", "進出口",
    # 自然/雜項
    "地震", "颱風", "天氣", "氣候", "廣告", "招募", "徵才", "白皮書",
    # 大宗商品
    "原油", "黃金", "鋰礦", "稀土", "礦產", "煤炭",
    # 人物生活（非商業）
    "裸辞", "降薪跳槽", "副業", "奥德赛", "职场极端",
]

# 標題有這些 → 幾乎確定是新創（配合有金額出現 → 直接 pre-accept）
STRONG_FUNDING_KW = ["融资", "融資", "募资", "募資", "完成", "获得投资", "獲得投資",
                     "pre-a", "天使轮", "天使輪", "种子轮", "種子輪",
                     "series a", "series b", "series c", "a轮", "b轮", "c轮", "a輪", "b輪", "c輪"]

# 標題有這些 → 直接 pre-accept（不需要有金額）
STRONG_STARTUP_KW = ["新创", "新創", "startup", "创业", "創業",
                     "加速器", "孵化器", "独角兽", "獨角獸",
                     "ipo", "上市", "掛牌", "招股", "挂牌",
                     "创始人", "創辦人", "founder",
                     "早期项目", "早期專案"]

# ── Google Sheets ──

def get_sheets_client():
    creds = Credentials.from_service_account_info(json.loads(GOOGLE_CREDENTIALS_JSON), scopes=SCOPES)
    return gspread.authorize(creds)

def get_sheet(tab_name: str) -> gspread.Worksheet:
    gc = get_sheets_client()
    return gc.open_by_key(SHEETS_ID).worksheet(tab_name)


# ── Content 解析：從 Sheets content 欄還原 title + summary ──

def parse_content_field(content: str) -> tuple[str, str]:
    """把 '標題：X\n摘要：Y\n內文：Z' 拆回 (title, summary)"""
    title, summary = "", ""
    if "標題：" in content:
        after = content.split("標題：", 1)[1]
        title = after.split("\n")[0].strip()
    if "摘要：" in content:
        after = content.split("摘要：", 1)[1]
        summary = after.split("內文：")[0].strip()[:300]
    return title, summary


# ── Stage 0：純 Python 規則分類（0ms） ──

def extract_funding_from_title(title: str) -> tuple[str, str]:
    """從標題抽出金額和輪次，回傳 (amount_raw, stage)"""
    amount = ""
    stage = ""

    amount_patterns = [
        r"([\d.]+\s*億[美台人]?幣?)",
        r"([\d.]+\s*千萬[美台人]?幣?)",
        r"([\d.]+\s*萬[美台人]?幣?)",
        r"(\$[\d.]+[MmBbKk])",
        r"(USD?\s*[\d.]+[MmBb])",
        r"([\d.]+\s*[Mm]illion)",
        r"([\d.]+亿[美人]?元?)",
        r"([\d.]+万[美人]?元?)",
    ]
    for p in amount_patterns:
        m = re.search(p, title, re.IGNORECASE)
        if m:
            amount = m.group(1)
            break

    stage_map = {
        "種子輪": ["種子輪", "seed round"],
        "天使輪": ["天使輪", "angel"],
        "Pre-A":  ["pre-a", "pre a", "prea"],
        "A輪":    ["a輪", "series a", "a round"],
        "B輪":    ["b輪", "series b", "b round"],
        "C輪":    ["c輪", "series c"],
        "D輪":    ["d輪", "series d"],
        "戰略投資": ["戰略投資", "strategic investment"],
        "IPO":    ["ipo", "上市", "掛牌"],
    }
    t = title.lower()
    for s, kws in stage_map.items():
        if any(kw in t for kw in kws):
            stage = s
            break

    return amount, stage


def rule_classify(title: str) -> str:
    """
    回傳: 'skip' | 'startup' | 'ambiguous'
    skip    → 不是新創，直接標記 processed，不送 Qwen
    startup → 確定是新創，跳過分類，直接進 Stage 2 提取
    ambiguous → 不確定，進 Stage 1 批次分類
    """
    t = title.lower()

    if any(kw in t for kw in RULE_SKIP_TITLE):
        return "skip"

    amount, _ = extract_funding_from_title(title)
    if amount and any(kw.lower() in t for kw in STRONG_FUNDING_KW):
        return "startup"

    if any(kw.lower() in t for kw in STRONG_STARTUP_KW):
        return "startup"

    return "ambiguous"


# ── Stage 1：批次標題分類（10篇/次 Qwen call） ──

def _call_ollama_raw(prompt: str, num_predict: int = 80) -> str:
    """直接回傳 Qwen 的文字輸出，不 parse JSON"""
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": num_predict},
    }
    resp = requests.post(f"{OLLAMA_BASE_URL}/api/generate", json=payload, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f"Ollama {resp.status_code}: {resp.text[:100]}")
    return resp.json().get("response", "")


def batch_classify_titles(items: list[dict]) -> list[int]:
    """
    送最多 10 篇標題給 Qwen，要它輸出哪些 index 是新創。
    items: [{"title": str, ...}]
    回傳: 是新創的 index 列表，e.g. [0, 2, 5]
    """
    lines = "\n".join(f"{i}. {item['title'][:80]}" for i, item in enumerate(items))
    prompt = (
        "Task: identify which headlines are about startups, innovative SMEs, or tech companies.\n"
        "INCLUDE: company funding, product launches, founder profiles, startup acquisitions, accelerator news.\n"
        "EXCLUDE: stock market data, government policy, macroeconomics, large public corps with no startup angle.\n"
        "Output ONLY a JSON array of 0-based indices to INCLUDE, e.g. [0,2,5]. If none, output [].\n\n"
        f"Headlines:\n{lines}\n\nOutput:"
    )
    try:
        raw = _call_ollama_raw(prompt, num_predict=60)
        m = re.search(r"\[[\d,\s]*\]", raw)
        if m:
            return json.loads(m.group(0))
    except Exception as e:
        logger.warning("batch_classify error: %s", e)
    return list(range(len(items)))  # fallback: 全部進 Stage 2


# ── Stage 2：單篇欄位提取（只送 title + 短摘要） ──

# Qwen 常見的「我不知道」佔位符，都視為無效
_PLACEHOLDER_NAMES = {
    "空", "empty", "n/a", "na", "none", "unknown", "未知",
    "中文名", "英文名", "公司名", "公司名稱", "english", "english or empty",
    "companyname", "company name", "名称", "名稱",
    "<actual company name>", "actual company name",
}

def _is_valid_company_name(name: str) -> bool:
    if not name or len(name.strip()) < 2:
        return False
    return name.strip().lower() not in _PLACEHOLDER_NAMES


def build_extract_prompt(title: str, summary: str, url: str, prefilled: dict) -> str:
    tw_hint = "Taiwan media: be generous, accept any tech/startup/innovation.\n" if any(d in url for d in TAIWAN_DOMAINS) else ""
    hints = []
    if prefilled.get("stage"):
        hints.append(f"Funding stage: {prefilled['stage']}")
    if prefilled.get("fundingAmountRaw"):
        hints.append(f"Funding amount: {prefilled['fundingAmountRaw']}")
    hint_text = ("Known info: " + ", ".join(hints) + "\n") if hints else ""

    # 根據標題語言決定 summary 語言要求
    is_chinese = any("一" <= c <= "鿿" for c in title)
    summary_ex = "公司本週完成Pre-A融資，專注AI數位員工平台" if is_chinese else "company raised $5B for defense AI systems"

    # 用具體示範值避免 Qwen 抄佔位符
    return (
        f"{tw_hint}{hint_text}"
        "Extract the main company from this article. Output JSON only (no markdown, no explanation).\n"
        "If you cannot identify a specific real company name, output: {}\n\n"
        "Example output:\n"
        '{"companyName":"未來式智能","companyNameEn":"MindOS",'
        '"description":"AI-powered digital employee platform for enterprise automation.",'
        f'"summary":"{summary_ex}",'
        '"industry":["AI","SaaS"],'
        '"stage":"Pre-A","fundingAmountRaw":"數百萬美元",'
        '"investors":["紅杉中國"],"founded":"2023","website":""}\n\n'
        "Now extract from:\n"
        f"Title: {title}\nSummary: {summary[:250]}"
    )


def call_ollama(prompt: str) -> dict | None:
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.05,
            "num_predict": 350,
            "top_p": 0.9,
            "repeat_penalty": 1.1,
        },
    }
    resp = requests.post(f"{OLLAMA_BASE_URL}/api/generate", json=payload, timeout=180)
    if resp.status_code != 200:
        raise RuntimeError(f"Ollama {resp.status_code}: {resp.text[:100]}")
    return parse_response(resp.json().get("response", ""))


_VALID_INDUSTRIES = {
    "AI", "SaaS", "FinTech", "醫療", "物流", "電商", "Mobility",
    "InsurTech", "GreenTech", "EdTech", "生技", "半導體", "機器人",
    "CyberSecurity", "區塊鏈", "硬體", "其他",
}

# Qwen 常見輸出變體 → 標準標籤
_INDUSTRY_ALIAS: dict[str, str] = {
    # AI
    "artificial intelligence": "AI", "人工智能": "AI", "机器学习": "AI",
    "deep learning": "AI", "llm": "AI", "大模型": "AI", "ai软件": "AI",
    # SaaS
    "cloud software": "SaaS", "enterprise software": "SaaS", "雲端軟體": "SaaS",
    "b2b software": "SaaS", "企業軟體": "SaaS",
    # FinTech
    "financial technology": "FinTech", "金融科技": "FinTech", "支付": "FinTech",
    # 醫療
    "healthcare": "醫療", "medical": "醫療", "health": "醫療", "medtech": "醫療",
    # 生技
    "biotech": "生技", "biotechnology": "生技", "life sciences": "生技",
    # 電商
    "ecommerce": "電商", "e-commerce": "電商", "retail": "電商",
    # 物流
    "logistics": "物流", "supply chain": "物流",
    # Mobility
    "autonomous": "Mobility", "electric vehicle": "Mobility", "ev": "Mobility",
    "transportation": "Mobility",
    # 機器人
    "robotics": "機器人", "automation": "機器人",
    # 半導體
    "semiconductor": "半導體", "chip": "半導體", "晶片": "半導體",
    # GreenTech
    "clean energy": "GreenTech", "renewable": "GreenTech", "cleantech": "GreenTech",
    # EdTech
    "education": "EdTech", "e-learning": "EdTech",
    # CyberSecurity
    "cybersecurity": "CyberSecurity", "security": "CyberSecurity",
    # 其他（defense / hardware etc）
    "defense": "其他", "defence": "其他", "hardware": "硬體",
}

_STAGE_PLACEHOLDERS = {
    "<a輪/b輪/種子輪/ipo/etc, or blank>", "a輪/b輪/種子輪/ipo/etc",
    "ipo/etc", "輪次", "blank", "empty", "etc",
}

# 常見 stage 別名 → 標準輪次
_STAGE_ALIAS: dict[str, str] = {
    "seed": "種子輪", "seed round": "種子輪", "种子轮": "種子輪",
    "angel": "天使輪", "天使轮": "天使輪",
    "pre-a": "Pre-A", "pre a": "Pre-A", "prea": "Pre-A",
    "series a": "A輪", "a轮": "A輪", "a round": "A輪",
    "series b": "B輪", "b轮": "B輪",
    "series c": "C輪", "c轮": "C輪",
    "series d": "D輪", "d轮": "D輪",
    "strategic": "戰略投資", "战略投资": "戰略投資",
    "ipo": "IPO", "initial public offering": "IPO",
    # 模糊 → 清空
    "early stage": "", "early": "", "late stage": "", "growth": "",
    "venture": "", "unknown": "",
}

def _normalize_result(d: dict) -> dict:
    """清理 Qwen 輸出中常見的格式問題"""
    # 拆分 "AI/SaaS" → ["AI", "SaaS"]，並解析別名
    raw_ind = d.get("industry", [])
    if isinstance(raw_ind, list):
        cleaned = []
        for item in raw_ind:
            for part in re.split(r"[/、,，]", str(item)):
                part = part.strip()
                part_lower = part.lower()
                if part in _VALID_INDUSTRIES:
                    cleaned.append(part)
                elif part_lower in _INDUSTRY_ALIAS:
                    cleaned.append(_INDUSTRY_ALIAS[part_lower])
                elif part:
                    matched = next((v for k, v in _INDUSTRY_ALIAS.items() if k in part_lower), None)
                    cleaned.append(matched if matched else "其他")
        industry = list(dict.fromkeys(cleaned))
    else:
        industry = []

    # 若 Qwen 沒輸出 industry，用 description + companyName 做 keyword 推導
    if not industry or industry == ["其他"]:
        combined = " ".join([
            d.get("description", ""), d.get("companyName", ""),
            d.get("companyNameEn", ""), d.get("summary", ""),
        ]).lower()
        derived = []
        for cat, kws in FIT_KEYWORDS.items():
            if any(kw.lower() in combined for kw in kws):
                # FIT_KEYWORDS 分類 → 映射到 industry 標籤
                ind_map = {
                    "Mobility": "Mobility", "InsurTech": "InsurTech", "FinTech": "FinTech",
                    "Healthcare": "醫療", "Logistics": "物流", "AI": "AI",
                    "SaaS": "SaaS", "Ecommerce": "電商",
                }
                if cat in ind_map:
                    derived.append(ind_map[cat])
        industry = list(dict.fromkeys(derived)) or ["其他"]

    d["industry"] = industry[:3]  # 最多 3 個

    # 正規化 stage
    stage = str(d.get("stage", "") or "")
    stage_lower = stage.lower()
    if stage_lower in _STAGE_PLACEHOLDERS or stage.startswith("<") or stage_lower in ("none", "null", "undefined"):
        d["stage"] = ""
    elif stage_lower in _STAGE_ALIAS:
        d["stage"] = _STAGE_ALIAS[stage_lower]
    elif stage not in {"種子輪", "天使輪", "Pre-A", "A輪", "B輪", "C輪", "D輪", "戰略投資", "IPO"}:
        # 嘗試部分匹配
        matched = next((v for k, v in _STAGE_ALIAS.items() if k in stage_lower), None)
        d["stage"] = matched if matched is not None else ""

    # 清除 fundingAmountRaw 佔位符
    amt = str(d.get("fundingAmountRaw", "") or "")
    if amt.lower() in ("none", "null", "empty", "blank", "undefined", ""):
        d["fundingAmountRaw"] = ""

    # investors 確保是 list
    inv = d.get("investors", [])
    if isinstance(inv, str):
        d["investors"] = [inv] if inv else []

    return d


def parse_response(text: str) -> dict | None:
    try:
        clean = re.sub(r"```json\n?|```\n?", "", text).strip()
        try:
            d = json.loads(clean)
            return _normalize_result(d) if isinstance(d, dict) else None
        except json.JSONDecodeError:
            pass
        m = re.search(r"\{[\s\S]*?\}", clean)
        if m:
            d = json.loads(m.group(0))
            return _normalize_result(d) if isinstance(d, dict) else None
    except Exception:
        pass
    return None


# ── Gemini（備用） ──

def call_gemini(prompt: str) -> dict | None:
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 600},
    }
    resp = requests.post(f"{GEMINI_ENDPOINT}?key={GEMINI_API_KEY}", json=payload, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Gemini {resp.status_code}: {resp.text[:150]}")
    try:
        raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        return None
    return parse_response(raw)


# ── Scoring ──

def calc_fit_score(s: dict) -> dict:
    combined = " ".join([
        s.get("description", ""),
        " ".join(s.get("industry", [])),
        " ".join(s.get("fitTags", [])),
        s.get("companyName", ""),
    ]).lower()
    score, tags = 0, []
    for cat, keywords in FIT_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw.lower() in combined)
        if hits:
            score += min(hits, 2)
            tags.append(cat)
    return {"fitScore": round(min(score / 2, 10) * 10) / 10, "fitTags": tags}


def normalize_funding(raw: str) -> int:
    if not raw:
        return 0
    t = raw.lower()
    m = re.search(r"[\d.]+", t)
    n = float(m.group(0)) if m else 0
    if "億" in t or "亿" in t:    n *= 100_000_000
    elif "千萬" in t:              n *= 10_000_000
    elif "萬" in t or "万" in t:  n *= 10_000
    elif "m" in t or "百萬" in t: n *= 1_000_000
    elif "k" in t or "千" in t:   n *= 1_000
    if "twd" in t or "台幣" in t:      n /= FX["TWD"]
    elif "cny" in t or "人民幣" in t:  n /= FX["CNY"]
    elif "sgd" in t:                   n /= FX["SGD"]
    return round(n / 100) * 100


def today_collection() -> str:
    return "startups_" + datetime.now().strftime("%Y-%m-%d")


# ── Main processor（三階段流程） ──

BATCH_SIZE = 10  # Stage 1 每批多少篇

def process_raw_articles_by_region(region: str, tab_name: str) -> dict:
    ws = get_sheet(tab_name)
    rows = ws.get_all_values()
    if len(rows) <= 1:
        logger.info("No articles in %s", tab_name)
        return {"saved": 0, "remaining": 0}

    unprocessed = []
    for i, row in enumerate(rows[1:], start=2):
        row_region    = row[4].strip() if len(row) > 4 else ""
        row_processed = row[6].strip().lower() if len(row) > 6 else "false"
        if row_region == region and row_processed in ("false", ""):
            unprocessed.append({"row": i, "data": row})

    logger.info("process [%s]: %d unprocessed", region, len(unprocessed))
    if not unprocessed:
        return {"saved": 0, "remaining": 0}

    region_limit = max(MAX_GEMINI_PER_RUN, 20) if region == "台灣" else MAX_GEMINI_PER_RUN
    batch = unprocessed[:region_limit]
    col_ref = today_collection()

    # ── Stage 0：規則分類 ──
    to_skip, to_extract, to_classify = [], [], []

    for item in batch:
        row_data = item["data"]
        title_raw = row_data[1] if len(row_data) > 1 else ""
        content   = row_data[2] if len(row_data) > 2 else ""
        title, summary = parse_content_field(content)
        if not title:
            title = title_raw

        amount, stage = extract_funding_from_title(title)
        item["title"]   = title
        item["summary"] = summary
        item["prefilled"] = {"fundingAmountRaw": amount, "stage": stage}

        verdict = rule_classify(title)
        if verdict == "skip":
            to_skip.append(item)
        elif verdict == "startup":
            to_extract.append(item)
        else:
            to_classify.append(item)

    logger.info(
        "Stage0 [%s]: skip=%d startup=%d ambiguous=%d",
        region, len(to_skip), len(to_extract), len(to_classify)
    )

    # 跳過的直接標 processed
    if to_skip:
        skip_rows = [[item["row"], "true"] for item in to_skip]
        for item in to_skip:
            ws.update_cell(item["row"], 7, "true")
        logger.info("   ⏭️  Rule-skip: %d articles", len(to_skip))
        time.sleep(1)

    # ── Stage 1：批次分類 ambiguous ──
    if to_classify:
        logger.info("Stage1: batch-classify %d ambiguous articles", len(to_classify))
        for batch_start in range(0, len(to_classify), BATCH_SIZE):
            chunk = to_classify[batch_start:batch_start + BATCH_SIZE]
            try:
                accepted_idx = batch_classify_titles(chunk)
                for idx, item in enumerate(chunk):
                    if idx in accepted_idx:
                        to_extract.append(item)
                    else:
                        ws.update_cell(item["row"], 7, "true")
                logger.info(
                    "   Stage1 batch %d-%d: %d/%d accepted",
                    batch_start, batch_start + len(chunk) - 1,
                    len(accepted_idx), len(chunk)
                )
            except Exception as e:
                logger.error("Stage1 batch error: %s — fallback to extract all", e)
                to_extract.extend(chunk)
            time.sleep(3)

    # ── Stage 2：個別提取 ──
    logger.info("Stage2: extract fields for %d confirmed startups", len(to_extract))
    saved = skipped = 0

    for j, item in enumerate(to_extract):
        row_data = item["data"]
        url      = row_data[0] if len(row_data) > 0 else ""
        source   = row_data[3] if len(row_data) > 3 else ""
        title    = item["title"]
        summary  = item["summary"]
        prefilled = item["prefilled"]

        try:
            logger.info("🔍 [%d/%d] %s", j + 1, len(to_extract), title[:60])
            prompt = build_extract_prompt(title, summary, url, prefilled)
            result = call_ollama(prompt)

            company = result.get("companyName", "") if result else ""
            if result and _is_valid_company_name(company):
                # 補回 prefilled 欄位（Qwen 可能漏掉）
                if not result.get("stage") and prefilled.get("stage"):
                    result["stage"] = prefilled["stage"]
                if not result.get("fundingAmountRaw") and prefilled.get("fundingAmountRaw"):
                    result["fundingAmountRaw"] = prefilled["fundingAmountRaw"]

                result["isStartup"]        = True
                result["region"]           = region
                result["sourceId"]         = source
                result["sourceUrl"]        = url
                result["extractedAt"]      = datetime.now(timezone.utc).isoformat()
                result["status"]           = "new"
                result["fundingAmountUSD"] = normalize_funding(result.get("fundingAmountRaw", ""))
                fd = calc_fit_score(result)
                result["fitScore"] = fd["fitScore"]
                result["fitTags"]  = fd["fitTags"]
                firestore_write(col_ref, result)
                saved += 1
                logger.info("   ✅ %s [score:%s]", company, result["fitScore"])
            else:
                skipped += 1
                reason = f"placeholder name: '{company}'" if company else "no companyName / empty result"
                logger.info("   ⚠️  Skip: %s", reason)

            ws.update_cell(item["row"], 7, "true")
            time.sleep(3)

        except Exception as e:
            logger.error("   ❌ %s", e)
            ws.update_cell(item["row"], 7, "error")
            time.sleep(2)

    remaining = max(0, len(unprocessed) - region_limit)
    logger.info("📊 [%s] saved=%d skipped=%d remaining=%d", region, saved, skipped, remaining)
    return {"saved": saved, "remaining": remaining}
