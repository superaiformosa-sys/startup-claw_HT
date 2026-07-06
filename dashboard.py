"""
和泰新創情報 Dashboard 產生器
從 Firebase 撈出所有 startups_* collection 的資料，產出一個純瀏覽用的靜態 HTML 儀表板：
可依時間區間（近3週/近4週/自訂）、地區、事業體標籤、產業篩選，不含爬蟲/流程觸發功能。

Usage:
    python dashboard.py                 # 產出 dashboard.html
    python dashboard.py my_dashboard.html
"""
import sys
import json
import logging
import datetime
from firebase_client import get_db
from config import TAG_LABELS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def load_all_startups() -> list[dict]:
    """掃描所有 startups_* collection（不限日期範圍——資料量小，全部載入交給前端篩選）。"""
    db = get_db()
    cols = sorted(c.id for c in db.collections() if c.id.startswith("startups_"))
    logger.info("Found %d startups_* collections: %s", len(cols), cols)

    records = []
    for col in cols:
        for doc in db.collection(col).stream():
            d = doc.to_dict()
            # Use the collection's batch date, not extractedAt — extractedAt is when the
            # AI scoring ran, which can be days later than the batch for backfilled tabs
            # (e.g. 0626 backfilled on 0703/0706), and would otherwise misfile records
            # under the wrong day.
            date = col.replace("startups_", "")

            group_fit = d.get("groupFitScore")
            if group_fit is None:
                group_fit = d.get("hotaiFitScore")
            startup_score = d.get("startupScore")
            if startup_score is None:
                startup_score = d.get("fitScore")

            records.append({
                "date":         date,
                "company":      d.get("companyName") or d.get("companyNameEn") or "-",
                "companyEn":    d.get("companyNameEn", ""),
                "region":       d.get("region", "-"),
                "stage":        d.get("stage", ""),
                "industry":     d.get("industry") or [],
                "tags":         d.get("fitTags") or [],
                "groupFit":     round(group_fit, 1) if group_fit is not None else None,
                "startupScore": round(startup_score, 1) if startup_score is not None else None,
                "fundingRaw":   d.get("fundingAmountRaw", ""),
                "fundingUSD":   d.get("fundingAmountUSD", 0),
                "url":          d.get("sourceUrl", ""),
                "title":        d.get("newsTitle", ""),
                "description":  d.get("description", ""),
                "summary":      d.get("summary", ""),
            })
    records.sort(key=lambda r: r["date"], reverse=True)
    logger.info("Loaded %d startup records", len(records))
    return records


_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>和泰新創情報 Dashboard</title>
<style>
:root {
  --surface-1:      #fcfcfb;
  --page:           #f9f9f7;
  --text-primary:   #0b0b0b;
  --text-secondary: #52514e;
  --muted:          #898781;
  --grid:           #e1e0d9;
  --baseline:       #c3c2b7;
  --border:         rgba(11,11,11,0.10);
  --series-1:       #2a78d6;
  --series-1-wash:  #eaf2fc;
  --seq-100:        #cde2fb;
  --seq-300:        #6da7ec;
  --seq-500:        #256abf;
  --good:           #0ca30c;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--page); color: var(--text-primary);
  font-family: system-ui, -apple-system, "Segoe UI", "Microsoft JhengHei", "Noto Sans TC", sans-serif;
  font-size: 14px; line-height: 1.5;
}
.wrap { max-width: 1180px; margin: 0 auto; padding: 24px 20px 60px; }

/* Header */
header.page-header { margin-bottom: 20px; }
header.page-header h1 { font-size: 1.5rem; font-weight: 700; margin: 0 0 4px; }
header.page-header .sub { color: var(--text-secondary); font-size: 0.85rem; }

/* Filter bar — one row, above the content it scopes */
.filters {
  background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px;
  padding: 14px 16px; margin-bottom: 20px;
}
.filter-row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 10px; }
.filter-row:last-child { margin-bottom: 0; }
.filter-label { font-size: 0.75rem; color: var(--muted); font-weight: 600; margin-right: 4px; min-width: 64px; }
.chip {
  display: inline-flex; align-items: center; gap: 4px; padding: 5px 12px; border-radius: 999px;
  border: 1px solid var(--border); background: var(--page); color: var(--text-secondary);
  font-size: 0.8rem; cursor: pointer; user-select: none; transition: all .12s;
}
.chip:hover { background: var(--series-1-wash); }
.chip.active {
  background: var(--series-1-wash); border-color: var(--series-1); color: var(--text-primary); font-weight: 700;
}
.chip.active::before { content: "✓"; color: var(--series-1); font-weight: 700; }
.date-input { border: 1px solid var(--border); border-radius: 6px; padding: 5px 8px; font-size: 0.8rem; color: var(--text-primary); background: var(--surface-1); }
.search-input {
  border: 1px solid var(--border); border-radius: 6px; padding: 6px 10px; font-size: 0.82rem;
  min-width: 220px; background: var(--surface-1); color: var(--text-primary);
}
.custom-range { display: flex; align-items: center; gap: 6px; font-size: 0.78rem; color: var(--muted); }

/* Tabs */
.tab-bar { display: flex; gap: 4px; border-bottom: 1px solid var(--border); margin-bottom: 20px; }
.tab {
  padding: 10px 4px; margin-right: 20px; font-size: 0.88rem; font-weight: 600; color: var(--muted);
  cursor: pointer; user-select: none; border-bottom: 2px solid transparent; margin-bottom: -1px;
}
.tab:hover { color: var(--text-primary); }
.tab.active { color: var(--series-1); border-bottom-color: var(--series-1); }
.tab-panel { }

/* KPI tiles */
.kpi-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 20px; }
.kpi-tile {
  background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px;
}
.kpi-tile .label { font-size: 0.75rem; color: var(--muted); margin-bottom: 6px; }
.kpi-tile .value { font-size: 1.7rem; font-weight: 700; }

/* Charts */
.chart-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 20px; }
.chart-card {
  background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 16px;
}
.chart-card h3 { font-size: 0.82rem; font-weight: 700; margin: 0 0 12px; text-transform: uppercase; letter-spacing: .04em; color: var(--text-secondary); }
.bar-row { display: flex; align-items: center; gap: 8px; margin-bottom: 7px; }
.bar-row .bar-label { width: 108px; flex-shrink: 0; font-size: 0.76rem; color: var(--text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.bar-track { flex: 1; background: var(--grid); border-radius: 3px; height: 16px; position: relative; overflow: hidden; }
.bar-fill { background: var(--series-1); height: 100%; border-radius: 3px; min-width: 3px; }
.bar-value { width: 30px; text-align: right; font-size: 0.76rem; font-weight: 700; color: var(--text-primary); flex-shrink: 0; }
.empty-chart { color: var(--muted); font-size: 0.8rem; padding: 8px 0; }

/* Article list */
.section-title { font-size: 0.95rem; font-weight: 700; margin: 0 0 12px; }
.list-meta { color: var(--muted); font-size: 0.78rem; margin-bottom: 10px; }
.card-list { display: flex; flex-direction: column; gap: 10px; }
.article-card {
  background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px;
}
.article-card .top-row { display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; margin-bottom: 6px; }
.article-card .company { font-weight: 700; font-size: 0.95rem; }
.article-card .company .en { font-weight: 400; color: var(--muted); font-size: 0.8rem; margin-left: 6px; }
.article-card .title-link { color: var(--text-secondary); font-size: 0.8rem; text-decoration: none; }
.article-card .title-link:hover { text-decoration: underline; }
.article-card .desc { font-size: 0.84rem; color: var(--text-secondary); margin: 6px 0; }
.badge-row { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 8px; }
.pill { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 0.72rem; font-weight: 600; }
.pill-region { background: #eef2fb; color: #3a4a6a; }
.pill-stage  { background: #fdebd0; color: #a05000; }
.pill-industry { background: var(--page); color: var(--text-secondary); border: 1px solid var(--border); }
.pill-tag { background: var(--series-1-wash); color: #184f95; border: 1px solid var(--series-1); }
.pill-fund { background: #d4f4e2; color: #1a7a3e; }
.score-badge { display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px; border-radius: 6px; font-size: 0.76rem; font-weight: 700; }
.score-lo  { background: var(--seq-100); color: #184f95; }
.score-mid { background: var(--seq-300); color: #0d366b; }
.score-hi  { background: var(--seq-500); color: #ffffff; }
.score-na  { color: var(--muted); font-size: 0.76rem; }
.empty-state { text-align: center; color: var(--muted); padding: 40px 0; font-size: 0.88rem; }

footer { text-align: center; color: var(--muted); font-size: 0.75rem; margin-top: 28px; padding-top: 16px; border-top: 1px solid var(--border); }

@media (max-width: 760px) {
  .kpi-row, .chart-grid { grid-template-columns: 1fr; }
}
</style>
</head>
<body>
<div class="wrap">

  <header class="page-header">
    <h1>和泰新創情報 Dashboard</h1>
    <div class="sub">產生時間：__GENERATED_AT__ ・ 共 __TOTAL_COUNT__ 篇（跨 __COL_COUNT__ 次爬蟲批次）・ 純瀏覽，不含爬蟲觸發</div>
  </header>

  <div class="filters">
    <div class="filter-row">
      <span class="filter-label">時間區間</span>
      <span class="chip" data-preset="1">最新一批</span>
      <span class="chip" data-preset="3">近 3 週</span>
      <span class="chip" data-preset="4">近 4 週</span>
      <span class="chip active" data-preset="all">全部</span>
      <span class="custom-range">
        自訂：<input type="date" id="dateFrom" class="date-input"> ~ <input type="date" id="dateTo" class="date-input">
      </span>
    </div>
    <div class="filter-row" id="regionFilters">
      <span class="filter-label">地區</span>
    </div>
    <div class="filter-row" id="tagFilters">
      <span class="filter-label">事業體</span>
    </div>
    <div class="filter-row" id="industryFilters">
      <span class="filter-label">產業</span>
    </div>
    <div class="filter-row">
      <span class="filter-label">搜尋</span>
      <input type="text" id="searchBox" class="search-input" placeholder="公司名稱 / 新聞標題關鍵字…">
    </div>
    <div class="filter-row">
      <span class="filter-label">排序依據</span>
      <span class="chip active" data-sort="groupFit">集團適配度</span>
      <span class="chip" data-sort="startupScore">新創推薦度</span>
    </div>
  </div>

  <div class="tab-bar">
    <span class="tab active" data-tab="overview">總覽</span>
    <span class="tab" data-tab="fullList">完整列表</span>
  </div>

  <div class="tab-panel" id="tabOverview">
    <div class="kpi-row" id="kpiRow"></div>

    <div class="chart-grid">
      <div class="chart-card"><h3>地區分佈</h3><div id="chartRegion"></div></div>
      <div class="chart-card"><h3>和泰事業體標籤分佈</h3><div id="chartTag"></div></div>
      <div class="chart-card"><h3>產業分佈</h3><div id="chartIndustry"></div></div>
      <div class="chart-card"><h3>融資輪次分佈</h3><div id="chartStage"></div></div>
    </div>

    <div class="section-title" id="summaryTitle">本週焦點摘要 Top 10（依集團適配度排序）</div>
    <div class="card-list" id="summaryList"></div>
  </div>

  <div class="tab-panel" id="tabFullList" style="display:none;">
    <div class="section-title" id="listTitle">完整文章列表（依集團適配度排序）</div>
    <div class="list-meta" id="listMeta"></div>
    <div class="card-list" id="cardList"></div>
  </div>

  <footer>
    本頁為靜態頁面，資料截至產生當下。如需更新，請重新執行 <code>python dashboard.py</code>。
  </footer>
</div>

<script>
const DATA = __DATA_JSON__;
const TAG_LABELS = __TAG_LABELS_JSON__;
const REGION_ORDER = ["台灣", "中國", "東南亞", "全球"];
const STAGE_ORDER = ["種子輪", "天使輪", "Pre-A", "A輪", "B輪", "C輪", "D輪", "戰略投資", "IPO"];

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
}

function fmtFunding(rec) {
  if (rec.fundingUSD) {
    const n = rec.fundingUSD;
    if (n >= 1e9) return "$" + (n / 1e9).toFixed(1) + "B";
    if (n >= 1e6) return "$" + (n / 1e6).toFixed(1) + "M";
    if (n >= 1e3) return "$" + (n / 1e3).toFixed(0) + "K";
    return "$" + n;
  }
  return rec.fundingRaw || "";
}

function scoreBadgeHtml(v, label) {
  if (v == null) return `<span class="score-na">${label} -</span>`;
  const cls = v >= 7 ? "score-hi" : (v >= 4 ? "score-mid" : "score-lo");
  return `<span class="score-badge ${cls}">${label} ${v.toFixed(1)}</span>`;
}

// ── Build filter chip lists from the data actually present ──
const allRegions = REGION_ORDER.filter(r => DATA.some(d => d.region === r));
// 事業體標籤：一定列出 config.py 目前定義的 12 類（就算目前 0 篇命中也列出來，讓使用者
// 看得到完整版圖，而不是只看到「目前剛好有資料」的少數幾類），命中數多的排前面；
// 舊版評分邏輯留下、不在 TAG_LABELS 裡的舊標籤（如 "AI"/"SaaS"）排在最後，一樣可篩選。
const tagCountsAll = {};
DATA.forEach(d => (d.tags || []).forEach(t => tagCountsAll[t] = (tagCountsAll[t] || 0) + 1));
const officialTags = Object.keys(TAG_LABELS).sort((a, b) => (tagCountsAll[b] || 0) - (tagCountsAll[a] || 0));
const legacyTags = Object.keys(tagCountsAll).filter(t => !TAG_LABELS[t]).sort((a, b) => tagCountsAll[b] - tagCountsAll[a]);
const allTags = [...officialTags, ...legacyTags];
const industryCounts = {};
DATA.forEach(d => (d.industry || []).forEach(i => industryCounts[i] = (industryCounts[i] || 0) + 1));
const allIndustries = Object.keys(industryCounts).sort((a, b) => industryCounts[b] - industryCounts[a]).slice(0, 12);

const state = {
  preset: "all",
  dateFrom: null,
  dateTo: null,
  region: null,          // null = 全部；否則單一地區（點台灣只顯示台灣，不是多選）
  tags: new Set(),      // empty = no tag filter applied (show all)
  industries: new Set(), // empty = no industry filter applied
  search: "",
  sortBy: "groupFit",   // "groupFit" (集團適配度) or "startupScore" (新創推薦度)
};

// 地區是單選（像分頁一樣）：點台灣只顯示台灣，「全部」重設回顯示所有地區
function buildRegionChips(containerId, items) {
  const container = document.getElementById(containerId);
  const allChip = document.createElement("span");
  allChip.textContent = "全部";
  allChip.addEventListener("click", () => { state.region = null; refreshRegionChips(); render(); });
  container.appendChild(allChip);
  const chips = items.map(item => {
    const chip = document.createElement("span");
    chip.textContent = item;
    chip.addEventListener("click", () => { state.region = item; refreshRegionChips(); render(); });
    container.appendChild(chip);
    return chip;
  });
  function refreshRegionChips() {
    allChip.className = "chip" + (state.region === null ? " active" : "");
    chips.forEach((chip, idx) => {
      chip.className = "chip" + (state.region === items[idx] ? " active" : "");
    });
  }
  refreshRegionChips();
}

const TAB_PANELS = { overview: "tabOverview", fullList: "tabFullList" };
document.querySelectorAll(".tab-bar .tab").forEach(tabEl => {
  tabEl.addEventListener("click", () => {
    document.querySelectorAll(".tab-bar .tab").forEach(t => t.classList.remove("active"));
    tabEl.classList.add("active");
    const target = tabEl.dataset.tab;
    Object.entries(TAB_PANELS).forEach(([key, panelId]) => {
      document.getElementById(panelId).style.display = key === target ? "" : "none";
    });
  });
});

const SORT_LABELS = { groupFit: "集團適配度", startupScore: "新創推薦度" };
document.querySelectorAll(".filters [data-sort]").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".filters [data-sort]").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    state.sortBy = btn.dataset.sort;
    render();
  });
});

// allowEmpty: an empty activeSet means "no filter applied" (every chip renders as active)
function buildChipRow(containerId, items, labelFn, activeSet, allowEmpty) {
  const container = document.getElementById(containerId);
  const chips = items.map(item => {
    const chip = document.createElement("span");
    chip.textContent = labelFn(item);
    chip.addEventListener("click", () => {
      if (activeSet.has(item)) activeSet.delete(item); else activeSet.add(item);
      refreshChips();
      render();
    });
    container.appendChild(chip);
    return chip;
  });
  function refreshChips() {
    chips.forEach((chip, idx) => {
      const item = items[idx];
      const isActive = activeSet.size === 0 && allowEmpty ? true : activeSet.has(item);
      chip.className = "chip" + (isActive ? " active" : "");
    });
  }
  refreshChips();
}

buildRegionChips("regionFilters", allRegions);
buildChipRow("tagFilters", allTags, t => TAG_LABELS[t] || t, state.tags, true);
buildChipRow("industryFilters", allIndustries, i => i, state.industries, true);

document.querySelectorAll(".filters [data-preset]").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".filters [data-preset]").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    state.preset = btn.dataset.preset;
    document.getElementById("dateFrom").value = "";
    document.getElementById("dateTo").value = "";
    state.dateFrom = null;
    state.dateTo = null;
    render();
  });
});

["dateFrom", "dateTo"].forEach(id => {
  document.getElementById(id).addEventListener("change", () => {
    document.querySelectorAll(".filters [data-preset]").forEach(b => b.classList.remove("active"));
    state.preset = "custom";
    state.dateFrom = document.getElementById("dateFrom").value || null;
    state.dateTo = document.getElementById("dateTo").value || null;
    render();
  });
});

document.getElementById("searchBox").addEventListener("input", e => {
  state.search = e.target.value.trim().toLowerCase();
  render();
});

function computeDateBounds() {
  if (state.preset === "custom") return [state.dateFrom, state.dateTo];
  if (state.preset === "all" || DATA.length === 0) return [null, null];
  const dates = DATA.map(d => d.date).sort();
  const latest = dates[dates.length - 1];
  const weeks = parseInt(state.preset, 10);
  const latestDate = new Date(latest + "T00:00:00Z");
  const fromDate = new Date(latestDate.getTime() - (weeks * 7 - 1) * 86400000);
  return [fromDate.toISOString().slice(0, 10), null];
}

function filterData() {
  const [from, to] = computeDateBounds();
  return DATA.filter(d => {
    if (from && d.date < from) return false;
    if (to && d.date > to) return false;
    if (state.region && d.region !== state.region) return false;
    if (state.tags.size > 0 && !(d.tags || []).some(t => state.tags.has(t))) return false;
    if (state.industries.size > 0 && !(d.industry || []).some(i => state.industries.has(i))) return false;
    if (state.search) {
      const hay = (d.company + " " + d.companyEn + " " + d.title).toLowerCase();
      if (!hay.includes(state.search)) return false;
    }
    return true;
  });
}

function renderBarChart(containerId, counts, order) {
  const el = document.getElementById(containerId);
  el.innerHTML = "";
  let entries = order
    ? order.filter(k => counts[k]).map(k => [k, counts[k]])
    : Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 10);
  if (entries.length === 0) {
    el.innerHTML = '<div class="empty-chart">篩選範圍內沒有資料</div>';
    return;
  }
  const max = Math.max(...entries.map(e => e[1]));
  entries.forEach(([label, count]) => {
    const row = document.createElement("div");
    row.className = "bar-row";
    const pct = Math.max((count / max) * 100, 4);
    row.innerHTML = `
      <div class="bar-label" title="${escapeHtml(label)}">${escapeHtml(label)}</div>
      <div class="bar-track"><div class="bar-fill" style="width:${pct}%"></div></div>
      <div class="bar-value">${count}</div>`;
    el.appendChild(row);
  });
}

function render() {
  const filtered = filterData();

  // KPI tiles
  const total = filtered.length;
  const scored = filtered.filter(d => d.groupFit != null);
  const avgFit = scored.length ? (scored.reduce((s, d) => s + d.groupFit, 0) / scored.length) : 0;
  const highFit = filtered.filter(d => (d.groupFit || 0) >= 7).length;
  // 只算 config.py 目前定義的 12 個正式事業體分類，不含舊版評分邏輯留下的標籤（如 "AI"/"SaaS"）
  const tagCoverage = new Set(filtered.flatMap(d => d.tags || []).filter(t => TAG_LABELS[t])).size;
  document.getElementById("kpiRow").innerHTML = `
    <div class="kpi-tile"><div class="label">篩選範圍文章數</div><div class="value">${total}</div></div>
    <div class="kpi-tile"><div class="label">平均集團適配度</div><div class="value">${avgFit.toFixed(1)}</div></div>
    <div class="kpi-tile"><div class="label">高適配文章數（≥7分）</div><div class="value">${highFit}</div></div>
    <div class="kpi-tile"><div class="label">涵蓋事業體數</div><div class="value">${tagCoverage} / ${Object.keys(TAG_LABELS).length}</div></div>
  `;

  // Charts
  const regionCounts = {};
  filtered.forEach(d => regionCounts[d.region] = (regionCounts[d.region] || 0) + 1);
  renderBarChart("chartRegion", regionCounts, REGION_ORDER);

  const tagCounts = {};
  filtered.forEach(d => (d.tags || []).forEach(t => tagCounts[TAG_LABELS[t] || t] = (tagCounts[TAG_LABELS[t] || t] || 0) + 1));
  renderBarChart("chartTag", tagCounts, null);

  const industryCountsF = {};
  filtered.forEach(d => (d.industry || []).forEach(i => industryCountsF[i] = (industryCountsF[i] || 0) + 1));
  renderBarChart("chartIndustry", industryCountsF, null);

  const stageCounts = {};
  filtered.forEach(d => { if (d.stage) stageCounts[d.stage] = (stageCounts[d.stage] || 0) + 1; });
  renderBarChart("chartStage", stageCounts, STAGE_ORDER);

  // Sorted once, shared by the Top 10 summary and the full list below it
  const sortLabel = SORT_LABELS[state.sortBy];
  const sorted = [...filtered].sort((a, b) => (b[state.sortBy] || 0) - (a[state.sortBy] || 0));

  document.getElementById("summaryTitle").textContent = `本週焦點摘要 Top 10（依${sortLabel}排序）`;
  document.getElementById("listTitle").textContent = `完整文章列表（依${sortLabel}排序）`;
  renderCardList("summaryList", sorted.slice(0, 10), "目前篩選範圍內沒有文章可摘要");
  document.getElementById("listMeta").textContent = `共 ${total} 篇符合篩選條件`;
  renderCardList("cardList", sorted, "沒有符合篩選條件的文章 — 試試放寬時間區間或篩選條件");
}

function articleCardHtml(d) {
  const tagsHtml = (d.tags || []).map(t => `<span class="pill pill-tag">${escapeHtml(TAG_LABELS[t] || t)}</span>`).join("");
  const industryHtml = (d.industry || []).map(i => `<span class="pill pill-industry">${escapeHtml(i)}</span>`).join("");
  const funding = fmtFunding(d);
  return `
    <div class="top-row">
      <div>
        <span class="company">${escapeHtml(d.company)}${d.companyEn && d.companyEn !== d.company ? `<span class="en">${escapeHtml(d.companyEn)}</span>` : ""}</span>
      </div>
      <div>${scoreBadgeHtml(d.groupFit, "集團適配")} ${scoreBadgeHtml(d.startupScore, "新創推薦")}</div>
    </div>
    <a class="title-link" href="${escapeHtml(d.url)}" target="_blank" rel="noopener">${escapeHtml(d.title || d.company)}</a>
    <div class="desc">${escapeHtml(d.description || d.summary || "")}</div>
    <div class="badge-row">
      <span class="pill pill-region">${escapeHtml(d.region)}</span>
      ${d.stage ? `<span class="pill pill-stage">${escapeHtml(d.stage)}</span>` : ""}
      ${funding ? `<span class="pill pill-fund">${escapeHtml(funding)}</span>` : ""}
      ${industryHtml}
      ${tagsHtml || '<span class="pill pill-industry">無明確事業體對應</span>'}
    </div>
    <div style="color:var(--muted);font-size:0.72rem;margin-top:6px;">${escapeHtml(d.date)}</div>
  `;
}

function renderCardList(containerId, records, emptyMessage) {
  const list = document.getElementById(containerId);
  list.innerHTML = "";
  if (records.length === 0) {
    list.innerHTML = `<div class="empty-state">${escapeHtml(emptyMessage)}</div>`;
    return;
  }
  records.forEach(d => {
    const card = document.createElement("div");
    card.className = "article-card";
    card.innerHTML = articleCardHtml(d);
    list.appendChild(card);
  });
}

render();
</script>
</body>
</html>
"""


def render_dashboard(records: list[dict]) -> str:
    generated_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    col_count = len({r["date"] for r in records})
    html = _HTML_TEMPLATE
    html = html.replace("__GENERATED_AT__", generated_at)
    html = html.replace("__TOTAL_COUNT__", str(len(records)))
    html = html.replace("__COL_COUNT__", str(col_count))
    html = html.replace("__DATA_JSON__", json.dumps(records, ensure_ascii=False))
    html = html.replace("__TAG_LABELS_JSON__", json.dumps(TAG_LABELS, ensure_ascii=False))
    return html


if __name__ == "__main__":
    out_path = sys.argv[1] if len(sys.argv) > 1 else "dashboard.html"
    records = load_all_startups()
    if not records:
        logger.error("No startup records found in Firebase — dashboard would be empty. Aborting.")
        sys.exit(1)
    html = render_dashboard(records)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info("✅ Dashboard written to %s (%d records)", out_path, len(records))
