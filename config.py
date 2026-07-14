import os
from dotenv import load_dotenv

load_dotenv()


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise EnvironmentError(
            f"Required environment variable '{name}' is not set. "
            "Add it to your .env file and restart."
        )
    return val


GEMINI_API_KEY          = os.environ.get("GEMINI_API_KEY", "")   # optional: Gemini fallback LLM
FIREBASE_PROJECT_ID     = _require_env("FIREBASE_PROJECT_ID")
SHEETS_ID               = _require_env("SHEETS_ID")
GOOGLE_CREDENTIALS_JSON = _require_env("GOOGLE_CREDENTIALS_JSON")

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent"

MAX_ARTICLES_PER_SOURCE = 30
MAX_GEMINI_PER_RUN      = 20

SOURCES = [
    # ── 台灣 ──
    # bnext direct RSS (bnext.com.tw/rss) 404s as of 2026-07 — site dropped the feed; routed via Google News site: query instead
    {"id": "bnext",      "name": "數位時代",            "rss": "https://news.google.com/rss/search?q=site:bnext.com.tw&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",                                          "region": "台灣", "enabled": True, "require_funding": True},
    {"id": "meet",       "name": "Meet 創業小聚",        "rss": "https://meet.bnext.com.tw/rss",                                                                                                   "region": "台灣", "enabled": True, "require_funding": True},
    {"id": "tc_tw",      "name": "TechCrunch TW",        "rss": "https://news.google.com/rss/search?q=新創+募資+台灣&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",                                             "region": "台灣", "enabled": True},
    {"id": "gn_tw1",     "name": "台灣新創 Google News", "rss": "https://news.google.com/rss/search?q=台灣+新創+融資+完成&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",                                         "region": "台灣", "enabled": True},
    {"id": "techorange",  "name": "科技報橘",            "rss": "https://buzzorange.com/techorange/feed/",                                                                                         "region": "台灣", "enabled": True, "require_funding": True},
    # inside.com.tw direct RSS (/feeds 404, /rss 403) as of 2026-07 — routed via Google News site: query instead
    {"id": "inside_tw",   "name": "Inside 硬塞",         "rss": "https://news.google.com/rss/search?q=site:inside.com.tw&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",                                        "region": "台灣", "enabled": True, "require_funding": True},
    {"id": "gn_tw_fund",  "name": "台灣融資 Google News", "rss": "https://news.google.com/rss/search?q=台灣+新創+(A輪+OR+B輪+OR+種子輪+OR+天使輪+OR+IPO)+融資&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",  "region": "台灣", "enabled": True},
    # ── 中國 ──
    {"id": "36kr",       "name": "36氪",               "rss": "https://36kr.com/feed",                                                               "region": "中國", "enabled": True},
    # lieyunwang.com domain no longer resolves (NXDOMAIN globally, confirmed via dns.google as of 2026-07) and isn't indexed by Google News either — disabled, no working route found
    {"id": "lieyunwang", "name": "獵雲網",              "rss": "https://www.lieyunwang.com/rss.xml",                                                  "region": "中國", "enabled": False},
    {"id": "cn_google",  "name": "中國新創 Google News", "rss": "https://news.google.com/rss/search?q=中国+创业+融资+startup&hl=zh-CN&gl=CN&ceid=CN:zh-Hans", "region": "中國", "enabled": True},
    {"id": "cn_google2", "name": "中國科技 Google News", "rss": "https://news.google.com/rss/search?q=中国+科技+独角兽+IPO&hl=zh-CN&gl=CN&ceid=CN:zh-Hans",   "region": "中國", "enabled": True},
    # ── 東南亞 ──
    # Nikkei Asia: broad Asia tech/business
    {"id": "nikkei_asia","name": "Nikkei Asia",           "rss": "https://asia.nikkei.com/rss/feed/nar",                                                                                     "region": "東南亞", "enabled": True, "require_funding": True},
    # Technode: reports Chinese tech news (excluded from SEA page filter)
    {"id": "technode",   "name": "Technode",               "rss": "https://technode.com/feed/",                                                                                               "region": "東南亞", "enabled": True, "require_funding": True},
    # Tech In Asia: best SEA startup coverage (SG/TH/MY/ID/VN/PH)
    # techinasia.com/feed is the correct URL but 403s here (Cloudflare/bot-block on this network) — routed via Google News site: query instead
    {"id": "techinasia", "name": "Tech In Asia",           "rss": "https://news.google.com/rss/search?q=site:techinasia.com&hl=en&gl=SG&ceid=SG:en",                                          "region": "東南亞", "enabled": True, "require_funding": True},
    # e27: Singapore-based SEA startup media
    {"id": "e27",        "name": "e27",                    "rss": "https://e27.co/feed/",                                                                                                     "region": "東南亞", "enabled": True, "require_funding": True},
    # Google News country-specific queries
    {"id": "sea_sg",     "name": "Singapore Startups",    "rss": "https://news.google.com/rss/search?q=startup+raised+funding+Singapore&hl=en&gl=SG&ceid=SG:en",                             "region": "東南亞", "enabled": True},
    {"id": "sea_th",     "name": "Thailand Startups",     "rss": "https://news.google.com/rss/search?q=startup+raised+funding+Thailand&hl=en&gl=TH&ceid=TH:en",                              "region": "東南亞", "enabled": True},
    {"id": "sea_my",     "name": "Malaysia Startups",     "rss": "https://news.google.com/rss/search?q=startup+raised+funding+Malaysia&hl=en&gl=MY&ceid=MY:en",                              "region": "東南亞", "enabled": True},
    {"id": "sea_id_vn",  "name": "ID/VN Startups",        "rss": "https://news.google.com/rss/search?q=startup+raised+funding+Indonesia+OR+Vietnam&hl=en&gl=SG&ceid=SG:en",                 "region": "東南亞", "enabled": True},
    {"id": "sea_apac",   "name": "APAC Startup Funding",  "rss": "https://news.google.com/rss/search?q=%22raised%22+%22series%22+startup+%22Southeast+Asia%22+%22million%22&hl=en&gl=SG&ceid=SG:en","region": "東南亞", "enabled": True},
    # ── 全球 ──
    {"id": "tc_startup", "name": "TechCrunch Startups",   "rss": "https://techcrunch.com/category/startups/feed/",                                      "region": "全球", "enabled": True},
    {"id": "venturebeat","name": "VentureBeat",            "rss": "https://venturebeat.com/category/business/feed/",                                     "region": "全球", "enabled": True},
    {"id": "sifted_eu",  "name": "Sifted (Europe)",        "rss": "https://sifted.eu/feed/",                                                             "region": "全球", "enabled": True},
    # Global Google News: no year in query avoids stale aggregate reports
    {"id": "global_gn",  "name": "全球新創 Google News",   "rss": "https://news.google.com/rss/search?q=startup+funding+%22series+A%22+OR+%22series+B%22+raised&hl=en&gl=US&ceid=US:en","region": "全球", "enabled": True},
    {"id": "global_gn2", "name": "全球科技 Google News",   "rss": "https://news.google.com/rss/search?q=unicorn+IPO+venture+capital+raised+million&hl=en&gl=US&ceid=US:en",              "region": "全球", "enabled": True},
]

REGION_MAP = {s["id"]: s["region"] for s in SOURCES}

# 和泰集團13大業務版圖對應關鍵字（用於 ML 特徵評分）
# 完整版圖：①汽車代理經銷 ②商用車 ③日本商用車 ④充電/能源 ⑤金融
#           ⑥租車 ⑦大陸經銷 ⑧車用產品 ⑨車體 ⑩產險 ⑪產機倉儲機器人
#           ⑫MaaS ⑬空調（大金台灣總代理）
FIT_KEYWORDS = {
    # ① 汽車代理經銷：和泰汽車/國都/北都/桃苗/中部/南都/高都/蘭揚
    "AutoRetail": [
        "汽車經銷", "車輛銷售", "車款", "dealer", "dealership", "DMS", "CRM dealer",
        "車商管理", "試乘", "車展", "新車銷售", "售後服務", "car retail",
        "toyota", "Toyota", "lexus", "Lexus", "和泰汽車", "和通汽車",   # 品牌/子公司
    ],
    # ② ③ 商用車（長源HINO、日本日野各公司）
    "CommercialVehicle": [
        "商用車", "卡車", "貨車", "巴士", "公車", "truck", "commercial vehicle",
        "bus", "HINO", "日野", "重型車", "fleet vehicle", "車隊採購",
    ],
    # ④ 充電樁及能源（起而行、旭電馳、充壹、和潤電能EVRun）
    "EV_Charging": [
        "電動車", "EV", "充電樁", "充電站", "充電網路", "氫能", "氫燃料", "氫電",
        "hydrogen", "FCEV", "換電", "battery swap", "CPO", "充電管理",
        "smart charging", "ev charging", "電動化", "electrification",
        "充電基礎設施", "charging infrastructure", "綠能", "能源管理",
    ],
    # ⑤ 金融（和潤企業6592、和勁企業）
    "AutoFinance": [
        "汽車貸款", "車貸", "融資租賃", "auto finance", "leasing", "分期付款",
        "殘值", "balloon payment", "fleet financing", "設備融資", "auto loan",
        "汽車金融", "vehicle finance", "動產擔保", "BNPL vehicle",
    ],
    # ⑥ 租車（和運租車）
    "CarRental_Fleet": [
        "租車", "car rental", "車隊管理", "fleet management", "長租", "短租",
        "subscription car", "汽車訂閱", "共享汽車", "car sharing", "leasing platform",
        "iRent", "和運",   # 和運旗下品牌
    ],
    # ⑧ 車用產品（車美仕Carmax、興聯科技、凱美士）
    "AutoProduct": [
        "車用電子", "汽車零件", "車載系統", "OBD", "行車記錄器", "dashcam",
        "automotive electronics", "aftermarket", "車用配件", "car accessory",
        "ADAS sensor", "車輛感測", "lidar", "雷達", "V2X", "車載AI",
    ],
    # ⑨ 車體（和泰車體製造/銷售、和泰巴士銷售）
    "VehicleBody": [
        "車體製造", "巴士車體", "bus body", "vehicle body", "特殊車輛",
        "electric bus", "電動巴士", "低地板巴士", "coach", "廂型車",
    ],
    # ⑩ 產險（和泰產險、和安保險）
    "InsurTech": [
        "保險科技", "insurtech", "UBI", "車險", "usage-based insurance",
        "telematics insurance", "理賠自動化", "核保AI", "再保", "數位保險",
        "嵌入式保險", "embedded insurance", "parametric insurance", "保費定價",
    ],
    # ⑪ 產機、倉儲、機器人（和泰豐田物料運搬TMHT）
    "IndustrialRobot": [
        "叉車", "堆高機", "forklift", "AGV", "AMR", "倉儲自動化",
        "warehouse automation", "物料搬運", "material handling", "工業機器人",
        "industrial robot", "智慧倉儲", "smart warehouse", "WMS", "自動倉",
        "TMHT", "Toyota Material", "toyota forklift", "toyota material handling",  # 和泰TMHT
    ],
    # ⑫ MaaS（yoxi、iRent、和泰聯網、去趣旅遊）
    "MaaS_Mobility": [
        "MaaS", "ride hailing", "叫車", "yoxi", "iRent", "共乘", "出行平台",
        "mobility service", "mobility as a service", "shared mobility",
        "旅遊規劃", "travel platform", "跨境旅遊", "trip planning",
        "和泰Pay", "points economy", "loyalty platform", "數位支付",
        "和雲", "去趣", "和泰聯網",  # 子品牌補全
    ],
    # ⑬ 空調（和泰興業 — 大金Daikin台灣總代理）
    "HVAC_Energy": [
        "空調", "冷氣", "HVAC", "大金", "Daikin", "冷凍空調", "智慧空調",
        "building energy", "能源效率", "EMS", "碳管理", "節能", "building automation",
    ],
    # AI / 數位平台（和泰AI中台、AI First戰略）— 橫跨多業務的水平能力
    "AI_DataPlatform": [
        "人工智慧", "機器學習", "AI", "大數據", "data platform", "個人化",
        "generative AI", "LLM", "AI platform", "數位轉型", "大模型",
        "預測分析", "predictive analytics", "API platform",
    ],
}


SKIP_KEYWORDS = ["廣告","sponsor","特別報導","白皮書","webinar","招募","徵才"]

# 廣泛RSS來源（非Google News查詢）需標題命中至少一個新創相關關鍵字才儲存
FUNDING_TITLE_KEYWORDS = [
    # 中文新創/融資
    "新創", "融資", "募資", "A輪", "B輪", "C輪", "D輪", "種子輪", "天使輪", "Pre-A",
    "IPO", "掛牌", "上市", "獲投", "創業", "新創公司", "科技新創",
    # 英文新創/融資
    "startup", "raised", "series a", "series b", "series c", "series d",
    "seed round", "funding", "ipo", "unicorn", "venture", "founder",
]
FX = {"TWD": 32, "CNY": 7.2, "JPY": 155, "SGD": 0.74, "MYR": 4.7}


# ══════════════════════════════════════════════════════════════════
# 以下集中管理原本散落在 ai_processor.py / weekly_report.py / dashboard.py /
# main.py 裡的關鍵字表、評分權重、門檻、顯示參數 —— 之後要調這些，只改這個檔案。
# ══════════════════════════════════════════════════════════════════

# ── Ollama ──
OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_MODEL = "qwen3:8b"

TAIWAN_DOMAINS = ["meet.bnext", "bnext.com", "inside.com.tw", "technews.tw", "news.google.com", "ctee.com.tw", "udn.com"]

# ── 和泰相關性最低門檻：groupFitScore < 此值的文章不寫入 Firebase ──
# 調高此值可讓 Firebase 更乾淨；調低可保留較多邊緣文章。
# groupFitScore 有 GROUP_FIT_BASELINE 基準分，零訊號文章會落在剛好等於基準分，
# 這個門檻等於「至少要有一點 Qwen/關鍵字/業務規則訊號」才留下來。
HOTAI_MIN_FIT_SCORE = 3.0

# ── Stage 0 規則分類（ai_processor.rule_classify，純 Python，不送 Qwen）──
# 標題出現任一關鍵字 → 直接跳過（不送 Qwen）
RULE_SKIP_TITLE = [
    # 股市
    "股市", "大盤", "成交額", "成交量", "漲跌", "休盤", "收盤", "開盤", "指數",
    "沪深", "恒指", "道指", "納指", "标普", "北向資金", "主力資金", "南向資金",
    "半日", "午間", "日线",  # 盤中報
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

# ── Qwen 輸出正規化：industry / stage 別名對應表 ──
VALID_INDUSTRIES = {
    "AI", "SaaS", "FinTech", "醫療", "物流", "電商", "Mobility",
    "InsurTech", "GreenTech", "EdTech", "生技", "半導體", "機器人",
    "CyberSecurity", "區塊鏈", "硬體", "其他",
}

# Qwen 常見輸出變體 → 標準標籤
INDUSTRY_ALIAS: dict[str, str] = {
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

STAGE_PLACEHOLDERS = {
    "<a輪/b輪/種子輪/ipo/etc, or blank>", "a輪/b輪/種子輪/ipo/etc",
    "ipo/etc", "輪次", "blank", "empty", "etc",
}

# 常見 stage 別名 → 標準輪次
STAGE_ALIAS: dict[str, str] = {
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

# ── 業務規則加分（ai_processor._business_rule_score）──
PREFERRED_STAGES  = {"A輪", "B輪", "C輪", "Pre-A", "戰略投資"}  # 和泰較可能投的輪次
PREFERRED_REGIONS = {"台灣", "東南亞"}                          # 地理偏好加分
REGION_BONUS_PREFERRED = 2.0
REGION_BONUS_CHINA     = 1.0
STAGE_BONUS_PREFERRED  = 2.0
STAGE_BONUS_EARLY      = 1.0   # 天使輪/種子輪
STAGE_BONUS_LATE       = 0.5   # IPO/D輪
CORE_BUSINESS_BONUS    = 2.0

# 直接命中和泰核心業務詞（含品牌名）→ +CORE_BUSINESS_BONUS
CORE_BUSINESS_HITS = [
    # MaaS / Mobility
    "mobility", "maas", "ride hailing", "叫車", "共乘", "yoxi", "irent",
    # InsurTech / 車險
    "insurtech", "telematics", "車險", "auto insurance",
    # 工業機器人 / 倉儲
    "forklift", "agv", "amr", "叉車", "倉儲", "tmht", "toyota material",
    # EV 充電
    "ev charging", "充電", "charging station",
    # 空調
    "daikin", "大金", "hvac",
    # 車體 / 商用車
    "bus body", "巴士", "hino", "日野",
    # 金融 / 租車
    "auto finance", "車貸", "租車", "car rental",
    # 品牌直接命中（排名最高加分）
    "toyota", "lexus",
]

# ── calc_scores 權重（groupFitScore / startupScore，ai_processor.py）──
GROUP_FIT_BASELINE = 2.5   # 基準分：避免零訊號文章顯示 0 分，讓整批文章看起來都不相關
GROUP_FIT_WEIGHT_QWEN    = 0.40
GROUP_FIT_WEIGHT_KEYWORD = 0.40
GROUP_FIT_WEIGHT_RULE    = 0.20
# Qwen 分數缺失時的 fallback 權重（重新分配掉 Qwen 那部分）
GROUP_FIT_FALLBACK_WEIGHT_KEYWORD = 0.60
GROUP_FIT_FALLBACK_WEIGHT_RULE    = 0.40

STARTUP_SCORE_WEIGHT_QWEN    = 0.40
STARTUP_SCORE_WEIGHT_FUNDING = 0.25
STARTUP_SCORE_WEIGHT_STAGE   = 0.20
STARTUP_SCORE_WEIGHT_QUALITY = 0.15
# Qwen 分數缺失時的 fallback 權重
STARTUP_SCORE_FALLBACK_WEIGHT_FUNDING = 0.40
STARTUP_SCORE_FALLBACK_WEIGHT_STAGE   = 0.35
STARTUP_SCORE_FALLBACK_WEIGHT_QUALITY = 0.25

# 融資金額分級（USD 門檻 → 0-10 分；由高到低比對，符合第一個門檻就採用）
FUNDING_SCORE_TIERS = [
    (100_000_000, 10.0),
    (50_000_000,  8.0),
    (10_000_000,  6.0),
    (1_000_000,   4.0),
]
FUNDING_SCORE_HAS_RAW_ONLY = 2.0   # 有寫金額但低於最低門檻（或無法解析數字）
FUNDING_SCORE_NONE         = 0.0   # 完全沒有金額資訊

# 融資輪次成熟度（0-10）
STAGE_MATURITY_SCORE = {
    "IPO": 10.0, "D輪": 9.0, "C輪": 8.0, "B輪": 7.0,
    "A輪": 6.0,  "Pre-A": 5.0, "天使輪": 4.0, "種子輪": 3.0,
    "戰略投資": 6.0,
}
STAGE_MATURITY_DEFAULT = 1.0

# 投資人資料 + 描述品質（0-10）：(門檻, 分數) 由高到低比對
INVESTOR_COUNT_QUALITY_TIERS = [(3, 6.0), (1, 3.0)]    # 投資人數 >= N → 該分數
DESC_LENGTH_QUALITY_TIERS    = [(80, 4.0), (40, 2.0)]  # 描述長度 >= N 字 → 該分數

# ── 和泰13大業務版圖標籤的中文顯示名稱（dashboard.py 用，key 需與 FIT_KEYWORDS 一致）──
TAG_LABELS = {
    "AutoRetail":        "汽車經銷",
    "CommercialVehicle": "商用車",
    "EV_Charging":       "充電/能源",
    "AutoFinance":       "汽車金融",
    "CarRental_Fleet":   "租車",
    "AutoProduct":       "車用產品",
    "VehicleBody":       "車體",
    "InsurTech":         "產險",
    "IndustrialRobot":   "產機/倉儲/機器人",
    "MaaS_Mobility":     "MaaS/移動服務",
    "HVAC_Energy":       "空調",
    "AI_DataPlatform":   "AI/數位平台",
}
assert set(TAG_LABELS) == set(FIT_KEYWORDS), "TAG_LABELS 和 FIT_KEYWORDS 的分類要一致，請同步更新"

# ── weekly_report.py 顯示參數 ──
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
# 週報「地區分頁」的排列順序（跟下面 pipeline 用的 REGIONS 順序不同，各自都是刻意設計）
REGION_ORDER = ["台灣", "東南亞", "中國", "全球"]
MIN_DISPLAY_GROUP_FIT = 3.0   # 低於此集團適配度的文章不顯示在報告中（呼應 HOTAI_MIN_FIT_SCORE）
REGION_DISPLAY_MAX = {"台灣": 20, "中國": 15, "東南亞": 10, "全球": 20}
REGION_DISPLAY_MIN = {"台灣": 10, "中國": 10, "東南亞":  5, "全球": 10}

# 東南亞頁面排除這些來源（實際報導中國/台灣內容，但被標記為東南亞）
SEA_EXCLUDE_SOURCES = {"technode", "cn_google", "cn_google2", "36kr", "lieyunwang",
                       "bnext", "meet", "tc_tw", "gn_tw1",
                       "techorange", "inside_tw", "gn_tw_fund"}  # 台灣來源一併排除

# weekly_report.guess_industry 用的產業關鍵字：跟 FIT_KEYWORDS 是不同用途，這個只是粗略
# 猜產業分類給週報統計用（不影響 Firebase 評分）
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

# ── main.py pipeline 參數 ──
REGIONS = ["台灣", "中國", "東南亞", "全球"]
# 台灣/中國單次 process_raw_articles_by_region 呼叫的預設上限，是為「每天都有機會被
# 下一次執行撿回來重跑」設計的。改成週爬蟲後，一週只有一個 sheet tab，沒處理完的文章
# 不會再被撿回來，所以把這兩區的上限拉高到週用量，一次呼叫處理完。
LOOPED_REGIONS    = ("台灣", "中國")
REGION_WEEKLY_CAP = 50   # 這兩區每次執行最多處理幾篇，避免無上限拖垮 Ollama
