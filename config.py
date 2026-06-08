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
    {"id": "bnext",      "name": "數位時代",            "rss": "https://www.bnext.com.tw/rss",                                                                                                    "region": "台灣", "enabled": True, "require_funding": True},
    {"id": "meet",       "name": "Meet 創業小聚",        "rss": "https://meet.bnext.com.tw/rss",                                                                                                   "region": "台灣", "enabled": True, "require_funding": True},
    {"id": "tc_tw",      "name": "TechCrunch TW",        "rss": "https://news.google.com/rss/search?q=新創+募資+台灣&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",                                             "region": "台灣", "enabled": True},
    {"id": "gn_tw1",     "name": "台灣新創 Google News", "rss": "https://news.google.com/rss/search?q=台灣+新創+融資+完成&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",                                         "region": "台灣", "enabled": True},
    {"id": "techorange",  "name": "科技報橘",            "rss": "https://buzzorange.com/techorange/feed/",                                                                                         "region": "台灣", "enabled": True, "require_funding": True},
    {"id": "inside_tw",   "name": "Inside 硬塞",         "rss": "https://www.inside.com.tw/feeds",                                                                                                 "region": "台灣", "enabled": True, "require_funding": True},
    {"id": "gn_tw_fund",  "name": "台灣融資 Google News", "rss": "https://news.google.com/rss/search?q=台灣+新創+(A輪+OR+B輪+OR+種子輪+OR+天使輪+OR+IPO)+融資&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",  "region": "台灣", "enabled": True},
    # ── 中國 ──
    {"id": "36kr",       "name": "36氪",               "rss": "https://36kr.com/feed",                                                               "region": "中國", "enabled": True},
    {"id": "lieyunwang", "name": "獵雲網",              "rss": "https://www.lieyunwang.com/rss.xml",                                                  "region": "中國", "enabled": True},
    {"id": "cn_google",  "name": "中國新創 Google News", "rss": "https://news.google.com/rss/search?q=中国+创业+融资+startup&hl=zh-CN&gl=CN&ceid=CN:zh-Hans", "region": "中國", "enabled": True},
    {"id": "cn_google2", "name": "中國科技 Google News", "rss": "https://news.google.com/rss/search?q=中国+科技+独角兽+IPO&hl=zh-CN&gl=CN&ceid=CN:zh-Hans",   "region": "中國", "enabled": True},
    # ── 東南亞 ──
    # Nikkei Asia: broad Asia tech/business
    {"id": "nikkei_asia","name": "Nikkei Asia",           "rss": "https://asia.nikkei.com/rss/feed/nar",                                                                                     "region": "東南亞", "enabled": True, "require_funding": True},
    # Technode: reports Chinese tech news (excluded from SEA page filter)
    {"id": "technode",   "name": "Technode",               "rss": "https://technode.com/feed/",                                                                                               "region": "東南亞", "enabled": True, "require_funding": True},
    # Tech In Asia: best SEA startup coverage (SG/TH/MY/ID/VN/PH)
    {"id": "techinasia", "name": "Tech In Asia",           "rss": "https://www.techinasia.com/feed",                                                                                          "region": "東南亞", "enabled": True, "require_funding": True},
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
