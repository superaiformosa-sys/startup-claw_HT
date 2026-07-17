import hashlib
import json
import re
import time
import logging
from datetime import datetime, timezone, timedelta
import requests
import gspread
from google.oauth2.service_account import Credentials
from config import (
    GEMINI_API_KEY, GEMINI_ENDPOINT, MAX_GEMINI_PER_RUN,
    SHEETS_ID, GOOGLE_CREDENTIALS_JSON, FIT_KEYWORDS, FX,
    OLLAMA_BASE_URL, OLLAMA_MODEL, TAIWAN_DOMAINS, HOTAI_MIN_FIT_SCORE,
    RULE_SKIP_TITLE, STRONG_FUNDING_KW, STRONG_STARTUP_KW,
    VALID_INDUSTRIES, INDUSTRY_ALIAS, STAGE_PLACEHOLDERS, STAGE_ALIAS,
    PREFERRED_STAGES, PREFERRED_REGIONS, REGION_BONUS_PREFERRED, REGION_BONUS_CHINA,
    STAGE_BONUS_PREFERRED, STAGE_BONUS_EARLY, STAGE_BONUS_LATE,
    CORE_BUSINESS_BONUS, CORE_BUSINESS_HITS,
    GROUP_FIT_BASELINE, GROUP_FIT_WEIGHT_QWEN, GROUP_FIT_WEIGHT_KEYWORD, GROUP_FIT_WEIGHT_RULE,
    GROUP_FIT_FALLBACK_WEIGHT_KEYWORD, GROUP_FIT_FALLBACK_WEIGHT_RULE,
    STARTUP_SCORE_WEIGHT_QWEN, STARTUP_SCORE_WEIGHT_FUNDING, STARTUP_SCORE_WEIGHT_STAGE,
    STARTUP_SCORE_WEIGHT_QUALITY, STARTUP_SCORE_FALLBACK_WEIGHT_FUNDING,
    STARTUP_SCORE_FALLBACK_WEIGHT_STAGE, STARTUP_SCORE_FALLBACK_WEIGHT_QUALITY,
    FUNDING_SCORE_TIERS, FUNDING_SCORE_HAS_RAW_ONLY, FUNDING_SCORE_NONE,
    STAGE_MATURITY_SCORE, STAGE_MATURITY_DEFAULT,
    INVESTOR_COUNT_QUALITY_TIERS, DESC_LENGTH_QUALITY_TIERS,
)
from firebase_client import firestore_write, get_db

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

# ── Google Sheets ──

def get_sheets_client():
    creds = Credentials.from_service_account_info(json.loads(GOOGLE_CREDENTIALS_JSON), scopes=SCOPES)
    return gspread.authorize(creds)

def get_sheet(tab_name: str) -> gspread.Worksheet:
    return _get_gc().open_by_key(SHEETS_ID).worksheet(tab_name)


_gc_cache: gspread.Client | None = None
_scored_ws_cache: gspread.Worksheet | None = None


def _get_gc() -> gspread.Client:
    global _gc_cache
    if _gc_cache is None:
        _gc_cache = get_sheets_client()
    return _gc_cache


_SCORED_TAB = "scored_results"
_SCORED_HEADER = [
    "extractedAt", "companyName", "companyNameEn", "region", "stage",
    "fundingAmountRaw", "fundingAmountUSD", "industry",
    "groupFitScore",   # 集團適配度（新）
    "startupScore",    # 新創推薦度（新）
    "mlScore", "ruleScore", "qwenGroupScore", "qwenRelScore",
    "fitTags", "sourceUrl", "newsTitle", "description", "summary",
]

def _get_or_create_scored_sheet(gc) -> gspread.Worksheet:
    global _scored_ws_cache
    if _scored_ws_cache is not None:
        return _scored_ws_cache
    ss = gc.open_by_key(SHEETS_ID)
    try:
        _scored_ws_cache = ss.worksheet(_SCORED_TAB)
    except gspread.WorksheetNotFound:
        _scored_ws_cache = ss.add_worksheet(title=_SCORED_TAB, rows=5000, cols=len(_SCORED_HEADER))
        _scored_ws_cache.append_row(_SCORED_HEADER)
        _scored_ws_cache.freeze(rows=1)
        logger.info("Created scored_results sheet tab")
    return _scored_ws_cache

def mirror_to_sheets(result: dict) -> None:
    """Append one scored startup row to the scored_results Sheets tab for human review."""
    try:
        gc = _get_gc()
        ws = _get_or_create_scored_sheet(gc)
        row = [
            result.get("extractedAt", ""),
            result.get("companyName", ""),
            result.get("companyNameEn", ""),
            result.get("region", ""),
            result.get("stage", ""),
            result.get("fundingAmountRaw", ""),
            result.get("fundingAmountUSD", ""),
            ", ".join(result.get("industry") or []),
            result.get("groupFitScore", ""),    # 集團適配度
            result.get("startupScore", ""),     # 新創推薦度
            result.get("mlScore", ""),
            result.get("ruleScore", ""),
            result.get("qwenGroupScore", ""),
            result.get("qwenRelScore", ""),
            ", ".join(result.get("fitTags") or []),
            result.get("sourceUrl", ""),
            result.get("newsTitle", ""),
            result.get("description", "")[:200],
            result.get("summary", "")[:200],
        ]
        ws.append_row(row, value_input_option="RAW")
    except Exception as e:
        logger.warning("mirror_to_sheets failed (non-fatal): %s", e)


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
        r"(\$[\d.]+\s*[Mm]n)",       # "$8 Mn" 縮寫（常見於印度/亞洲財經媒體）
        r"(\$[\d.]+\s*[Bb]n)",
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
        "think": False,
        "options": {"temperature": 0.0, "num_predict": num_predict},
    }
    resp = requests.post(f"{OLLAMA_BASE_URL}/api/generate", json=payload, timeout=200)
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
    except requests.Timeout:
        # On timeout, treat all items as accepted (same conservative fallback as other exceptions).
        # Returning [] would silently mark all items as processed with nothing written to Firebase.
        logger.error("Stage1 Ollama timeout — fallback: treating all %d as startup", len(items))
        return list(range(len(items)))
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

# Hotai itself (the client doing the evaluating) — not a startup, must never be the extracted company.
_HOTAI_SELF_NAMES = {
    "和泰集團", "和泰汽車", "和泰", "hotai group", "hotai motor", "hotai",
}

def _is_valid_company_name(name: str) -> bool:
    if not name or len(name.strip()) < 2:
        return False
    return name.strip().lower() not in _PLACEHOLDER_NAMES


def _is_hotai_self_reference(*names: str) -> bool:
    return any(n and n.strip().lower() in _HOTAI_SELF_NAMES for n in names)


_HOTAI_CONTEXT = (
    "Hotai Group (和泰集團) is Taiwan's largest automotive conglomerate with 13 business verticals: "
    "① Auto retail — Toyota/Lexus/Hino exclusive distributor (38.6% market share), "
    "  dealerships: 國都/北都/桃苗/中部/南都/高都/蘭揚; "
    "② Commercial vehicles — 長源汽車 Taiwan HINO trucks & buses; "
    "③ Japan commercial vehicles — 南關東日野/北海道日野/東北海道/宮城日野/福島日野; "
    "④ EV charging & energy — EVRun (起而行綠能/旭電馳/充壹/和潤電能), U-POWER investment, MIRAI hydrogen; "
    "⑤ Finance — 和潤企業 (auto loans & leasing, largest non-bank financier in Taiwan), 和勁企業; "
    "⑥ Car rental — 和運租車 (6,000+ vehicle fleet, iRent car-sharing 10K vehicles); "
    "⑦ China auto retail — 和通汽車投資 + mainland dealerships; "
    "⑧ Auto products — 車美仕Carmax / 興聯科技 / 凱美士 (automotive electronics & accessories); "
    "⑨ Vehicle body — 和泰車體製造/銷售, 和泰巴士銷售 (bus body manufacturing); "
    "⑩ P&C Insurance — 和泰產險, 和安保險; "
    "⑪ Industrial machinery & warehouse robotics — 和泰豐田物料運搬TMHT (Toyota Material Handling Taiwan); "
    "⑫ MaaS — yoxi ride-hailing, iRent, 和泰聯網, 去趣 travel app (3.5M downloads), "
    "   和泰Pay + 和泰Points (3B+ points, 4M members); "
    "⑬ HVAC — 和泰興業 (Daikin exclusive distributor in Taiwan). "
    "Strategic priorities 2026: AI First (和泰AI中台), MaaS ecosystem expansion, "
    "EV/hydrogen transition, InsurTech digitization, warehouse automation, smart tourism."
)


def build_extract_prompt(title: str, summary: str, url: str, prefilled: dict) -> str:
    hints = []
    if prefilled.get("stage"):
        hints.append(f"Funding stage: {prefilled['stage']}")
    if prefilled.get("fundingAmountRaw"):
        hints.append(f"Funding amount: {prefilled['fundingAmountRaw']}")
    hint_text = ("Known info: " + ", ".join(hints) + "\n") if hints else ""

    is_chinese = any("一" <= c <= "鿿" for c in title)
    desc_ex    = "AI驅動的企業數位員工平台，專注自動化解決方案。" if is_chinese else "AI-powered digital employee platform for enterprise automation."
    summary_ex = "公司本週完成Pre-A融資，專注AI數位員工平台。" if is_chinese else "company raised $5M for enterprise AI platform."
    lang_instr = (
        "IMPORTANT: Write 'description' and 'summary' in Traditional Chinese (繁體中文).\n"
        if is_chinese else
        "IMPORTANT: Write 'description' and 'summary' in English.\n"
    )

    return (
        f"{hint_text}"
        f"{lang_instr}"
        "Extract the main company from this startup/tech article. Output JSON only (no markdown, no explanation).\n"
        "If you cannot identify a specific real company name, output: {}\n"
        "Never output Hotai (和泰集團/Hotai Group) as the company — Hotai is the client evaluating fit, "
        "not a startup being reported on. If the article lists multiple startups with no single clear "
        "primary subject (e.g. a roundup/listicle), output: {}\n\n"
        "Example output:\n"
        '{"companyName":"未來式智能","companyNameEn":"MindOS",'
        f'"description":"{desc_ex}",'
        f'"summary":"{summary_ex}",'
        '"industry":["AI","SaaS"],'
        '"stage":"Pre-A","fundingAmountRaw":"數百萬美元",'
        '"investors":["紅杉中國"],"founded":"2023","website":"",'
        '"relevanceScore":7,"hotaiFitScore":6}\n\n'
        "relevanceScore scale (0-10) — news quality:\n"
        "  9-10: confirmed funding round with amount and investors named\n"
        "  7-8:  confirmed funding round or product launch, some details\n"
        "  5-6:  startup mentioned but funding/product unclear\n"
        "  3-4:  tangentially related (industry report, large corp with startup angle)\n"
        "  0-2:  irrelevant (macro, stock market, policy, large public corp)\n\n"
        f"hotaiFitScore scale (0-10) — strategic fit for {_HOTAI_CONTEXT}\n"
        "  9-10: core fit — directly addresses Hotai's main businesses "
        "(EV/charging, MaaS/mobility, ADAS/connected-vehicle, auto InsurTech, auto finance)\n"
        "  7-8:  strong fit — AI/data platform, loyalty/payment ecosystem, smart tourism, fleet management\n"
        "  5-6:  adjacent fit — general InsurTech, FinTech, logistics, mobility-adjacent tech\n"
        "  3-4:  weak fit — general SaaS/consumer tech with possible Hotai synergies\n"
        "  0-2:  no clear strategic fit for Hotai\n\n"
        "Now extract from:\n"
        f"Title: {title}\nSummary: {summary[:250]}"
    )


def build_classify_and_extract_prompt(title: str, summary: str, prefilled: dict) -> str:
    """Combined Stage1+Stage2 prompt: classify whether startup, and if so extract all fields."""
    hints = []
    if prefilled.get("stage"):
        hints.append(f"Funding stage: {prefilled['stage']}")
    if prefilled.get("fundingAmountRaw"):
        hints.append(f"Funding amount: {prefilled['fundingAmountRaw']}")
    hint_text = ("Known info: " + ", ".join(hints) + "\n") if hints else ""

    is_chinese = any("一" <= c <= "鿿" for c in title)
    desc_ex    = "AI驅動的企業數位員工平台，專注自動化解決方案。" if is_chinese else "AI-powered digital employee platform for enterprise automation."
    summary_ex = "公司本週完成Pre-A融資，專注AI數位員工平台。" if is_chinese else "company raised $5M for enterprise AI platform."
    lang_instr = (
        "IMPORTANT: Write 'description' and 'summary' in Traditional Chinese (繁體中文).\n"
        if is_chinese else
        "IMPORTANT: Write 'description' and 'summary' in English.\n"
    )

    return (
        f"{hint_text}"
        f"{lang_instr}"
        "Classify and extract. Is this article about a startup, innovative SME, or tech company?\n"
        "INCLUDE: company funding, product launches, founder profiles, acquisitions, accelerator news.\n"
        "EXCLUDE: stock market data, government policy, macroeconomics, large public corps with no startup angle.\n\n"
        'If NOT a startup → output ONLY: {"isStartup":false}\n'
        "If you cannot identify a specific real company name → output ONLY: {\"isStartup\":false}\n"
        "Never output Hotai (和泰集團/Hotai Group) as the company — Hotai is the client evaluating fit, "
        "not a startup being reported on. If the article lists multiple startups with no single clear "
        "primary subject (e.g. a roundup/listicle) → output ONLY: {\"isStartup\":false}\n\n"
        "If IS a startup → extract the main company. Output JSON only (no markdown, no explanation).\n"
        "Example output:\n"
        '{"isStartup":true,"companyName":"未來式智能","companyNameEn":"MindOS",'
        f'"description":"{desc_ex}",'
        f'"summary":"{summary_ex}",'
        '"industry":["AI","SaaS"],'
        '"stage":"Pre-A","fundingAmountRaw":"數百萬美元",'
        '"investors":["紅杉中國"],"founded":"2023","website":"",'
        '"relevanceScore":7,"hotaiFitScore":6}\n\n'
        "relevanceScore scale (0-10) — news quality:\n"
        "  9-10: confirmed funding round with amount and investors named\n"
        "  7-8:  confirmed funding round or product launch, some details\n"
        "  5-6:  startup mentioned but funding/product unclear\n"
        "  3-4:  tangentially related\n"
        "  0-2:  irrelevant\n\n"
        f"hotaiFitScore scale (0-10) — strategic fit for {_HOTAI_CONTEXT}\n"
        "  9-10: core fit — EV/charging, MaaS/mobility, ADAS, auto InsurTech, auto finance\n"
        "  7-8:  strong fit — AI/data platform, loyalty/payment, smart tourism, fleet management\n"
        "  5-6:  adjacent fit — general InsurTech, FinTech, logistics, mobility-adjacent\n"
        "  3-4:  weak fit — general SaaS/consumer tech\n"
        "  0-2:  no clear strategic fit for Hotai\n\n"
        f"Title: {title}\nSummary: {summary[:250]}"
    )


def _call_ollama_with_retry(prompt: str, max_retries: int = 3) -> dict | None:
    """call_ollama with exponential backoff on Timeout."""
    for attempt in range(1, max_retries + 1):
        try:
            return call_ollama(prompt)
        except requests.Timeout:
            wait = 2 ** attempt
            logger.warning("Ollama timeout (attempt %d/%d) — retry in %ds", attempt, max_retries, wait)
            if attempt < max_retries:
                time.sleep(wait)
        except Exception as e:
            logger.error("Ollama error: %s", e)
            return None
    logger.error("Ollama failed after %d retries", max_retries)
    return None


def call_ollama(prompt: str) -> dict | None:
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "format": "json",   # 強制 Ollama 輸出合法 JSON，大幅減少 parse 失敗
        "think": False,
        "options": {
            "temperature": 0.05,
            "num_predict": 200,
            "top_p": 0.9,
            "repeat_penalty": 1.1,
        },
    }
    # Measured ~167s for a warm-model classify+extract call on this CPU-only host (~7 tok/s);
    # 180s left almost no headroom and was tripping on cold-start / load spikes (see logs 2026-06-08).
    resp = requests.post(f"{OLLAMA_BASE_URL}/api/generate", json=payload, timeout=300)
    if resp.status_code != 200:
        raise RuntimeError(f"Ollama {resp.status_code}: {resp.text[:100]}")
    return parse_response(resp.json().get("response", ""))


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
                if part in VALID_INDUSTRIES:
                    cleaned.append(part)
                elif part_lower in INDUSTRY_ALIAS:
                    cleaned.append(INDUSTRY_ALIAS[part_lower])
                elif part:
                    matched = next((v for k, v in INDUSTRY_ALIAS.items() if k in part_lower), None)
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
    if stage_lower in STAGE_PLACEHOLDERS or stage.startswith("<") or stage_lower in ("none", "null", "undefined"):
        d["stage"] = ""
    elif stage_lower in STAGE_ALIAS:
        d["stage"] = STAGE_ALIAS[stage_lower]
    elif stage not in {"種子輪", "天使輪", "Pre-A", "A輪", "B輪", "C輪", "D輪", "戰略投資", "IPO"}:
        # 嘗試部分匹配
        matched = next((v for k, v in STAGE_ALIAS.items() if k in stage_lower), None)
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
        # 找第一個 { 到最後一個 }，避免 non-greedy *? 遇到 nested dict 就截斷
        start = clean.find("{")
        end = clean.rfind("}")
        if start != -1 and end > start:
            d = json.loads(clean[start:end + 1])
            return _normalize_result(d) if isinstance(d, dict) else None
    except Exception:
        pass
    return None


# ── Gemini（主力，額度用完後 fallback 回 Qwen） ──

class GeminiQuotaExceeded(Exception):
    """Gemini free-tier quota exhausted (HTTP 429) — stop trying Gemini for the rest of this run."""


def call_gemini(prompt: str) -> dict | None:
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 600},
    }
    resp = requests.post(f"{GEMINI_ENDPOINT}?key={GEMINI_API_KEY}", json=payload, timeout=30)
    if resp.status_code == 429:
        raise GeminiQuotaExceeded(resp.text[:200])
    if resp.status_code != 200:
        raise RuntimeError(f"Gemini {resp.status_code}: {resp.text[:150]}")
    try:
        raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        return None
    return parse_response(raw)


_gemini_exhausted = False  # set True for the rest of this process once quota runs out


def call_llm_with_retry(prompt: str, max_retries: int = 3) -> dict | None:
    """Try Gemini first (fast, offloads the slow local Qwen); once its quota is
    exhausted for the day, fall back to Qwen for the remainder of this run."""
    global _gemini_exhausted
    if GEMINI_API_KEY and not _gemini_exhausted:
        try:
            return call_gemini(prompt)
        except GeminiQuotaExceeded:
            logger.warning("Gemini quota exhausted — switching to Qwen for the rest of this run")
            _gemini_exhausted = True
        except Exception as e:
            logger.warning("Gemini error, falling back to Qwen for this call: %s", e)
    return _call_ollama_with_retry(prompt, max_retries=max_retries)


# ── Scoring ──

def _calc_keyword_score(result: dict) -> tuple[float, list[str]]:
    """
    FIT_KEYWORDS 關鍵字命中分（0-10）＋業務標籤，供 groupFitScore 使用。
    原始命中 0-2pt → normalize 到 0-10：一篇文章通常只會扣中 1-2 個事業體分類
    （少有文章橫跨 4 個以上分類），舊版除以 4.0 等於要求「命中範圍要廣」才能拿高分，
    對單一分類命中很深（例如同時出現多個充電樁關鍵字）的文章反而不公平，
    這裡改成命中 2 個分類就給滿分，獎勵「命中深度」而非「命中廣度」。
    """
    combined = " ".join([
        result.get("description", ""),
        result.get("summary", ""),
        " ".join(result.get("industry", [])),
        result.get("companyName", ""),
        result.get("companyNameEn", ""),
        result.get("newsTitle", ""),
    ]).lower()

    kw_raw = 0.0
    tags: list[str] = []
    for cat, keywords in FIT_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw.lower() in combined)
        if hits:
            kw_raw += min(hits * 0.5, 1.0)
            tags.append(cat)
    # 0-2 raw → 0-10 normalized
    kw_score = min(kw_raw / 2.0 * 10, 10.0)
    return round(kw_score, 1), tags


# Backward-compat wrapper（舊呼叫方不受影響）
def calc_ml_score(result: dict) -> tuple[float, list[str]]:
    """舊介面保留：回傳關鍵字命中分（0-10）及業務標籤。"""
    return _calc_keyword_score(result)


def _business_rule_score(result: dict) -> float:
    """
    業務規則加分（0-10），作為第三個評分維度：
      地區加分：台灣/東南亞各 +REGION_BONUS_PREFERRED；中國 +REGION_BONUS_CHINA
      輪次偏好：A/B/C/Pre-A/戰略 +STAGE_BONUS_PREFERRED；天使/種子 +STAGE_BONUS_EARLY；IPO/D輪 +STAGE_BONUS_LATE
      業務名直接命中：companyName 含和泰子公司相關業務詞 +CORE_BUSINESS_BONUS
    """
    score = 0.0
    region = result.get("region", "")
    if region in PREFERRED_REGIONS:
        score += REGION_BONUS_PREFERRED
    elif region == "中國":
        score += REGION_BONUS_CHINA

    stage = result.get("stage", "")
    if stage in PREFERRED_STAGES:
        score += STAGE_BONUS_PREFERRED
    elif stage in {"天使輪", "種子輪"}:
        score += STAGE_BONUS_EARLY
    elif stage in {"IPO", "D輪"}:
        score += STAGE_BONUS_LATE

    # 直接命中和泰核心業務詞（含品牌名）
    combined = " ".join([
        result.get("companyName", ""), result.get("description", ""),
        " ".join(result.get("industry", [])),
    ]).lower()
    if any(h in combined for h in CORE_BUSINESS_HITS):
        score += CORE_BUSINESS_BONUS
    return min(score, 10.0)


def calc_scores(result: dict) -> dict:
    """
    兩維度評分系統：
    ┌─────────────────┬───────────────────────────────────────────────────────┐
    │ groupFitScore   │ 集團適配度（0-10）：與和泰13大業務版圖的契合程度      │
    │ （集團適配度）  │ = 2.5（基準分）+ 40% Qwen語意 + 40% FIT_KEYWORDS命中 │
    │                 │   + 20% 業務規則                                     │
    ├─────────────────┼───────────────────────────────────────────────────────┤
    │ startupScore    │ 新創推薦度（0-10）：新創本身的品質與投資關注度        │
    │ （新創推薦度）  │ = 40% Qwen新聞可信度 + 25% 融資金額                  │
    │                 │   + 20% 輪次成熟度 + 15% 投資人/描述品質             │
    └─────────────────┴───────────────────────────────────────────────────────┘
    groupFitScore 的 GROUP_FIT_BASELINE 基準分：完全零訊號的文章（Qwen判0分+無關鍵字命中+
    無業務規則加分）以前會落到 0 分，讓整批文章看起來「全部都不合適」而失去參考價值；
    多數被留下來的文章多少都跟新創/投資沾邊，基準分讓分數分布往中間靠、保留區間內的相對
    排序，同時把 HOTAI_MIN_FIT_SCORE 拉高，讓真正零訊號的文章還是會被濾掉。
    同時保留 hotaiFitScore / fitScore 作為 backward-compat alias。
    所有權重/門檻數字定義在 config.py，方便之後調整。
    """
    kw_score, tags = _calc_keyword_score(result)
    rule_score     = _business_rule_score(result)

    def _qwen(field: str) -> float | None:
        try:
            v = result.get(field)
            if v is not None:
                return max(0.0, min(10.0, float(v)))
        except (TypeError, ValueError):
            pass
        return None

    def _tier_score(value: float, tiers: list[tuple[float, float]], default: float) -> float:
        """tiers 是 (門檻, 分數) 由高到低排列的清單；回傳第一個 value >= 門檻 的分數。"""
        for threshold, score in tiers:
            if value >= threshold:
                return score
        return default

    qwen_hotai = _qwen("hotaiFitScore")   # Qwen 原始：對和泰策略的語意判斷
    qwen_rel   = _qwen("relevanceScore")  # Qwen 原始：新聞可信度/品質

    # ── 集團適配度 (groupFitScore) ──
    if qwen_hotai is not None:
        group_fit = (GROUP_FIT_BASELINE
                     + GROUP_FIT_WEIGHT_QWEN * qwen_hotai
                     + GROUP_FIT_WEIGHT_KEYWORD * kw_score
                     + GROUP_FIT_WEIGHT_RULE * rule_score)
    else:
        group_fit = (GROUP_FIT_BASELINE
                     + GROUP_FIT_FALLBACK_WEIGHT_KEYWORD * kw_score
                     + GROUP_FIT_FALLBACK_WEIGHT_RULE * rule_score)

    # ── 新創推薦度 (startupScore) ──
    # 各子分數先 normalize 到 0-10，再加權

    # A. 融資金額 (0-10)
    funding_usd = result.get("fundingAmountUSD") or 0
    funding_raw = result.get("fundingAmountRaw") or ""
    fund_sc = _tier_score(funding_usd, FUNDING_SCORE_TIERS,
                          FUNDING_SCORE_HAS_RAW_ONLY if funding_raw else FUNDING_SCORE_NONE)

    # B. 融資輪次成熟度 (0-10)
    stage_sc = STAGE_MATURITY_SCORE.get(result.get("stage", ""), STAGE_MATURITY_DEFAULT)

    # C. 投資人資料 + 描述品質 (0-10)
    investors = result.get("investors") or []
    desc      = result.get("description") or ""
    quality   = (_tier_score(len(investors), INVESTOR_COUNT_QUALITY_TIERS, 0.0)
                 + _tier_score(len(desc), DESC_LENGTH_QUALITY_TIERS, 0.0))
    quality   = min(quality, 10.0)

    if qwen_rel is not None:
        startup = (STARTUP_SCORE_WEIGHT_QWEN * qwen_rel
                   + STARTUP_SCORE_WEIGHT_FUNDING * fund_sc
                   + STARTUP_SCORE_WEIGHT_STAGE * stage_sc
                   + STARTUP_SCORE_WEIGHT_QUALITY * quality)
    else:
        # Qwen 缺失時重新分配權重
        startup = (STARTUP_SCORE_FALLBACK_WEIGHT_FUNDING * fund_sc
                   + STARTUP_SCORE_FALLBACK_WEIGHT_STAGE * stage_sc
                   + STARTUP_SCORE_FALLBACK_WEIGHT_QUALITY * quality)

    clamp = lambda v: min(round(v, 1), 10.0)

    return {
        "groupFitScore":  clamp(group_fit),      # 集團適配度（新）
        "startupScore":   clamp(startup),         # 新創推薦度（新）
        "fitTags":        tags,
        "mlScore":        kw_score,               # 關鍵字命中分（透明度備查）
        "ruleScore":      round(rule_score, 1),   # 業務規則分（透明度備查）
        "qwenGroupScore": qwen_hotai,             # Qwen 原始和泰判斷
        "qwenRelScore":   qwen_rel,               # Qwen 原始新聞品質
        # Backward-compat aliases（舊 Firebase 文件、舊 weekly_report 仍可讀）
        "hotaiFitScore":  clamp(group_fit),
        "fitScore":       clamp(startup),
    }


# Backward-compat wrapper（舊呼叫方不受影響）
def calc_fit_score(result: dict) -> dict:
    """舊介面保留，內部呼叫 calc_scores。"""
    d = calc_scores(result)
    # 補舊欄位名
    d["qwenScore"]      = d.get("qwenRelScore")
    d["hotaiQwenScore"] = d.get("qwenGroupScore")
    return d


def normalize_funding(raw: str) -> int:
    if not raw:
        return 0
    t = raw.lower()
    m = re.search(r"[\d.]+", t)
    n = float(m.group(0)) if m else 0
    if "億" in t or "亿" in t:                     n *= 100_000_000
    elif "千萬" in t:                               n *= 10_000_000
    elif "萬" in t or "万" in t:                   n *= 10_000
    elif "billion" in t or re.search(r"\db\b", t): n *= 1_000_000_000
    elif "m" in t or "百萬" in t:                  n *= 1_000_000
    elif "k" in t or "千" in t:                    n *= 1_000
    if "twd" in t or "台幣" in t:      n /= FX["TWD"]
    elif "cny" in t or "人民幣" in t:  n /= FX["CNY"]
    elif "sgd" in t:                   n /= FX["SGD"]
    return round(n / 100) * 100


def today_collection() -> str:
    return "startups_" + datetime.now().strftime("%Y-%m-%d")


def collection_for_tab(tab_name: str) -> str:
    """Derive Firebase collection name from the Sheets tab name.
    'raw_2026-05-20' → 'startups_2026-05-20'
    Falls back to today if tab_name has no date suffix.
    """
    if tab_name and tab_name.startswith("raw_"):
        return "startups_" + tab_name[4:]
    return today_collection()


def _already_saved_this_week(collection: str, company: str) -> bool:
    """公司名（AI 抽出的正式名稱，比標題關鍵字指紋可靠）這週是否已經存過。
    標題層的指紋去重（見 scraper.py 的 fingerprint dedup）只在單次 process_raw_articles_by_region
    呼叫內比對，遇到跨執行（例如 Gemini 額度上限分批跑）或標題沒寫金額/輪次的狀況會漏接——
    這裡改成直接查 Firebase 本週這個 collection 裡是否已有同名公司，不受這兩個限制影響。"""
    company = company.strip()
    if not company:
        return False
    from google.cloud.firestore_v1.base_query import FieldFilter
    db = get_db()
    docs = db.collection(collection).where(filter=FieldFilter("companyName", "==", company)).limit(1).stream()
    return any(True for _ in docs)


def _persist_startup(item: dict, result: dict, ws, col_ref: str, region: str) -> str:
    """Validate, score, and persist a startup result to Firebase and Sheets.
    Returns 'saved' or 'skipped'. Raises on unexpected errors (caller handles)."""
    row_data  = item["data"]
    url       = row_data[0] if len(row_data) > 0 else ""
    source    = row_data[3] if len(row_data) > 3 else ""
    prefilled = item.get("prefilled", {})

    if not url:
        logger.warning("   ⚠️  Row %d has no URL — marking skipped", item["row"])
        ws.update_cell(item["row"], 7, "skipped")
        return "skipped"

    company    = result.get("companyName", "")
    company_en = result.get("companyNameEn", "")
    if not _is_valid_company_name(company):
        reason = f"placeholder name: '{company}'" if company else "no companyName"
        logger.info("   ⚠️  Skip: %s", reason)
        ws.update_cell(item["row"], 7, "skipped")
        return "skipped"
    if _is_hotai_self_reference(company, company_en):
        logger.info(
            "   ⚠️  Skip: extracted Hotai itself (self-reference), not a startup — '%s' / '%s'",
            company, company_en,
        )
        ws.update_cell(item["row"], 7, "skipped")
        return "skipped"

    if not result.get("stage") and prefilled.get("stage"):
        result["stage"] = prefilled["stage"]
    if not result.get("fundingAmountRaw") and prefilled.get("fundingAmountRaw"):
        result["fundingAmountRaw"] = prefilled["fundingAmountRaw"]

    result["isStartup"]        = True
    result["region"]           = region
    result["sourceId"]         = source
    result["sourceUrl"]        = url
    result["newsTitle"]        = item.get("title", "")   # 原始新聞標題（供 HTML 報告顯示）
    result["extractedAt"]      = datetime.now(timezone.utc).isoformat()
    result["status"]           = "new"
    result["fundingAmountUSD"] = normalize_funding(result.get("fundingAmountRaw", ""))
    fd = calc_scores(result)

    def _clamp(v: float) -> float:
        return max(0.0, min(10.0, v))

    # ── 主要兩維度分數 ──
    result["groupFitScore"]  = _clamp(fd["groupFitScore"])   # 集團適配度
    result["startupScore"]   = _clamp(fd["startupScore"])    # 新創推薦度
    result["fitTags"]        = fd["fitTags"]
    result["mlScore"]        = _clamp(fd["mlScore"])
    result["ruleScore"]      = _clamp(fd["ruleScore"])
    if fd["qwenGroupScore"] is not None:
        result["qwenGroupScore"] = _clamp(fd["qwenGroupScore"])
    if fd["qwenRelScore"] is not None:
        result["qwenRelScore"] = _clamp(fd["qwenRelScore"])
    # Backward-compat aliases（舊 weekly_report / 舊 Firebase 文件仍可讀）
    result["hotaiFitScore"]  = result["groupFitScore"]
    result["fitScore"]       = result["startupScore"]

    # ── 和泰相關性過濾：集團適配度 < HOTAI_MIN_FIT_SCORE → 不寫入 Firebase ──
    # groupFitScore 整合 Qwen 語意（50%）＋關鍵字命中（30%）＋業務規則（20%）
    if result["groupFitScore"] < HOTAI_MIN_FIT_SCORE:
        logger.info(
            "   ⏭️  集團不相關 (groupFit=%.1f < %.1f): %s",
            result["groupFitScore"], HOTAI_MIN_FIT_SCORE, company[:50]
        )
        ws.update_cell(item["row"], 7, "filtered")
        return "skipped"

    if _already_saved_this_week(col_ref, company):
        logger.info("   🔁 Skip: '%s' already saved to %s this week (duplicate coverage from another source)",
                    company, col_ref)
        ws.update_cell(item["row"], 7, "true")
        return "skipped"

    doc_id = hashlib.md5(url.encode()).hexdigest()[:16]
    firestore_write(col_ref, result, doc_id=doc_id)
    mirror_to_sheets(result)
    ws.update_cell(item["row"], 7, "true")
    logger.info("   ✅ %s [fit:%.1f hotai:%.1f]", company, result["fitScore"], result["hotaiFitScore"])
    return "saved"


# ── Main processor（三階段流程） ──

BATCH_SIZE = 10  # Stage 1 每批多少篇（保留供參考，已不在主流程中使用）

def process_raw_articles_by_region(region: str, tab_name: str, limit: int | None = None) -> dict:
    ws = get_sheet(tab_name)
    rows = ws.get_all_values()
    if len(rows) <= 1:
        logger.info("No articles in %s", tab_name)
        return {"saved": 0, "remaining": 0, "processed": 0}

    unprocessed = []
    for i, row in enumerate(rows[1:], start=2):
        row_region    = row[4].strip() if len(row) > 4 else ""
        row_processed = row[6].strip().lower() if len(row) > 6 else "false"
        if row_region == region and row_processed in ("false", ""):
            unprocessed.append({"row": i, "data": row})

    logger.info("process [%s]: %d unprocessed", region, len(unprocessed))
    if not unprocessed:
        return {"saved": 0, "remaining": 0, "processed": 0}

    # ── 指紋去重：同一輪次+同一金額的文章多半是同一則融資被不同來源報導 ──
    # 在切 batch（週上限）之前先做，避免重複文章吃掉寶貴的處理額度。
    # 風險：不同公司剛好同週同輪次同金額會被誤判為重複而略過（機率低，可接受）。
    seen_fingerprints: dict[tuple, int] = {}
    fingerprint_dup_rows: list[int] = []
    deduped_unprocessed = []
    for item in unprocessed:
        row_data = item["data"]
        title_raw = row_data[1] if len(row_data) > 1 else ""
        title, _ = parse_content_field(row_data[2] if len(row_data) > 2 else "")
        if not title:
            title = title_raw
        amount, stage = extract_funding_from_title(title)
        amount_usd = normalize_funding(amount) if amount else 0
        fp = (stage, amount_usd) if (stage and amount_usd) else None
        if fp and fp in seen_fingerprints:
            fingerprint_dup_rows.append(item["row"])
            logger.info("   🔁 Fingerprint dup of row %d (%s, $%s): %s",
                        seen_fingerprints[fp], stage, amount_usd, title[:50])
            continue
        if fp:
            seen_fingerprints[fp] = item["row"]
        deduped_unprocessed.append(item)
    if fingerprint_dup_rows:
        ws.batch_update([
            {"range": f"G{r}", "values": [["true"]]} for r in fingerprint_dup_rows
        ])
        logger.info("   🔁 Fingerprint-deduped %d articles [%s] — skipped Qwen", len(fingerprint_dup_rows), region)
        time.sleep(1)
    unprocessed = deduped_unprocessed

    region_limit = limit if limit is not None else (30 if region == "台灣" else MAX_GEMINI_PER_RUN)
    batch = unprocessed[:region_limit]
    col_ref = collection_for_tab(tab_name)  # Bug1 fix: derive from tab_name, not today's date

    # ── Stage 0：規則分類 ──
    to_skip, to_extract, to_classify = [], [], []

    NINETY_DAYS_AGO = datetime.now(timezone.utc) - timedelta(days=90)

    for item in batch:
        row_data = item["data"]
        title_raw    = row_data[1] if len(row_data) > 1 else ""
        content      = row_data[2] if len(row_data) > 2 else ""
        published_at = row_data[7] if len(row_data) > 7 else ""  # col 8 added by scraper
        title, summary = parse_content_field(content)
        if not title:
            title = title_raw

        # Date filter: skip articles older than 90 days (Google News sometimes recirculates old articles)
        if published_at:
            try:
                from email.utils import parsedate_to_datetime
                pub_dt = parsedate_to_datetime(published_at)
                if pub_dt.tzinfo is None:
                    pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                if pub_dt < NINETY_DAYS_AGO:
                    logger.info("   📅 Skip old article (%s): %s", published_at[:16], title[:50])
                    item["title"]     = title
                    item["summary"]   = summary
                    item["prefilled"] = {}
                    to_skip.append(item)
                    continue
            except Exception:
                pass  # unparseable date → proceed normally

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

    # 跳過的直接標 processed（batch update 減少 Sheets API 呼叫數）
    if to_skip:
        ws.batch_update([
            {"range": f"G{item['row']}", "values": [["true"]]}
            for item in to_skip
        ])
        logger.info("   ⏭️  Rule-skip: %d articles", len(to_skip))
        time.sleep(1)

    # ── Stage 1+2：ambiguous 文章合併分類＋提取（一次 LLM 呼叫） ──
    saved = skipped = errors = 0
    stage1_2_accepted: list[tuple[dict, dict]] = []

    if to_classify:
        logger.info("Stage1+2: classify+extract %d ambiguous articles", len(to_classify))
        rejected_rows_s1: list[int] = []
        for j, item in enumerate(to_classify):
            url = item["data"][0] if len(item["data"]) > 0 else ""
            if not url:
                rejected_rows_s1.append(item["row"])
                continue
            try:
                logger.info("  🔎 [%d/%d] %s", j + 1, len(to_classify), item["title"][:60])
                prompt = build_classify_and_extract_prompt(
                    item["title"], item["summary"], item["prefilled"]
                )
                result = call_llm_with_retry(prompt)
                if result is None:
                    # Ollama unreachable — leave unprocessed so next run retries
                    logger.error("    ❌ Ollama unavailable, skipping row %d (will retry next run)", item["row"])
                    errors += 1
                elif result.get("isStartup") is False:
                    rejected_rows_s1.append(item["row"])
                    logger.info("    ✗ not startup")
                else:
                    stage1_2_accepted.append((item, result))
                    logger.info("    ✓ %s", result.get("companyName", "?")[:40])
            except Exception as e:
                logger.error("    ❌ Stage1+2 row %d: %s", item["row"], e)
                ws.update_cell(item["row"], 7, "error")
                errors += 1
            time.sleep(3)
        if rejected_rows_s1:
            ws.batch_update([
                {"range": f"G{r}", "values": [["true"]]} for r in rejected_rows_s1
            ])
        logger.info(
            "Stage1+2 [%s]: accepted=%d rejected=%d",
            region, len(stage1_2_accepted), len(rejected_rows_s1)
        )

    # ── Stage 2：Stage0 直接確認的 startup 提取欄位 ──
    logger.info("Stage2: extract fields for %d Stage-0 confirmed startups", len(to_extract))

    for j, item in enumerate(to_extract):
        url = item["data"][0] if len(item["data"]) > 0 else ""

        if not url:
            logger.warning("   ⚠️  Row %d has no URL — marking skipped", item["row"])
            ws.update_cell(item["row"], 7, "skipped")
            skipped += 1
            continue

        try:
            logger.info("🔍 [%d/%d] %s", j + 1, len(to_extract), item["title"][:60])
            prompt = build_extract_prompt(item["title"], item["summary"], url, item["prefilled"])
            result = call_llm_with_retry(prompt)

            if result is None:
                logger.warning("   ⚠️  Ollama parse failure for: %s", item["title"][:60])
                ws.update_cell(item["row"], 7, "error")
                errors += 1
                time.sleep(2)
                continue

            outcome = _persist_startup(item, result, ws, col_ref, region)
            if outcome == "saved":
                saved += 1
            else:
                skipped += 1

        except Exception as e:
            logger.error("   ❌ row %d: %s", item["row"], e)
            ws.update_cell(item["row"], 7, "error")
            errors += 1
            time.sleep(2)
            continue

        time.sleep(3)

    # ── Stage1+2 accepted 儲存 ──
    if stage1_2_accepted:
        logger.info("Saving %d Stage1+2 accepted startups", len(stage1_2_accepted))
    for item, result in stage1_2_accepted:
        try:
            outcome = _persist_startup(item, result, ws, col_ref, region)
            if outcome == "saved":
                saved += 1
            else:
                skipped += 1
        except Exception as e:
            logger.error("   ❌ Stage1+2 save row %d: %s", item["row"], e)
            ws.update_cell(item["row"], 7, "error")
            errors += 1

    remaining = max(0, len(unprocessed) - region_limit)
    logger.info("📊 [%s] saved=%d skipped=%d errors=%d remaining=%d",
                region, saved, skipped, errors, remaining)
    if errors:
        logger.error("⚠️  %d rows marked 'error' in [%s] — review and retry manually", errors, region)
    return {"saved": saved, "remaining": remaining, "errors": errors, "processed": len(batch)}
