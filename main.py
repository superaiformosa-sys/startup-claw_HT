import sys
import time
import logging
import datetime
import subprocess
import requests
from scraper import run_all_scrapers
from ai_processor import process_raw_articles_by_region, OLLAMA_MODEL
from weekly_report import load_all_tabs, analyze_rows, render_html
from email_sender import send_weekly_report

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434"

def ensure_ollama_running(timeout: int = 30) -> bool:
    try:
        requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        logger.info("Ollama already running")
        return True
    except Exception:
        pass

    logger.info("Ollama not running — starting ollama serve ...")
    subprocess.Popen(
        ["ollama", "serve"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(2)
        try:
            requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
            logger.info("Ollama started successfully")
            return True
        except Exception:
            pass

    logger.error("Ollama failed to start within %ds", timeout)
    return False

def warm_up_ollama_model(timeout: int = 280) -> None:
    """Force the model into memory with a tiny throwaway generate call.
    Without this, the first real classify+extract call pays both the
    model-load cost AND inference cost inside the same request timeout —
    that's what caused the timeouts in the 2026-06-08 run."""
    try:
        start = time.time()
        requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": "Hi", "stream": False,
                  "options": {"num_predict": 1}},
            timeout=timeout,
        )
        logger.info("Ollama model warmed up (%.1fs)", time.time() - start)
    except Exception as e:
        logger.warning("Ollama warm-up failed (will proceed anyway): %s", e)

REGIONS = ["台灣", "中國", "東南亞", "全球"]

def step1_scrape(tab_name=None):
    start = datetime.datetime.now()
    logger.info("Step1 START: %s", start.isoformat())
    tab_name = run_all_scrapers(tab_name)
    duration = (datetime.datetime.now() - start).total_seconds()
    logger.info("Step1 DONE: %.1fs, tab: %s", duration, tab_name)
    return tab_name

def step2_ai(tab_name=None):
    start = datetime.datetime.now()
    logger.info("Step2 START (AI+Firebase)")
    if not ensure_ollama_running():
        logger.error("Step2 ABORT — Ollama could not be started")
        return
    warm_up_ollama_model()
    if tab_name is None:
        tab_name = "raw_" + datetime.datetime.now().strftime("%Y-%m-%d")
    total_saved = total_errors = 0
    failed_regions = []
    for region in REGIONS:
        try:
            logger.info("Processing region: %s", region)
            result = process_raw_articles_by_region(region, tab_name)
            total_saved  += result.get("saved", 0)
            total_errors += result.get("errors", 0)
        except Exception as e:
            logger.error("region %s FAILED: %s", region, e)
            failed_regions.append(region)
    duration = (datetime.datetime.now() - start).total_seconds()
    logger.info("Step2 DONE: %.1fs | saved=%d errors=%d", duration, total_saved, total_errors)
    if failed_regions or total_errors:
        logger.error("⚠️  Step2 had failures — regions_failed=%s row_errors=%d",
                     failed_regions, total_errors)

def step3_report(tab_name: str | None = None, send_email: bool = False):
    start = datetime.datetime.now()
    logger.info("Step3 START (weekly report)")
    try:
        rows = load_all_tabs()
        if not rows and tab_name:
            from weekly_report import load_single_tab
            rows = load_single_tab(tab_name)
    except Exception as e:
        logger.error("Step3 load error: %s", e)
        rows = []

    if not rows:
        logger.warning("Step3: no rows found — skipping report")
        return

    stats = analyze_rows(rows)
    from weekly_report import load_scored_map
    from ai_processor import collection_for_tab
    scored_map = load_scored_map(collection_for_tab(tab_name or ""))
    stats["hotai_top"] = sorted(
        [d for d in scored_map.values() if d.get("hotaiFitScore") is not None],
        key=lambda d: d["hotaiFitScore"], reverse=True,
    )[:10]
    html = render_html(tab_name or "all_tabs", rows, stats, scored_map=scored_map)

    if send_email:
        ok = send_weekly_report(html)
        if not ok:
            logger.error("Step3: email failed — report saved to local file")
    else:
        filename = f"weekly_report_{datetime.date.today()}.html"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Step3: report saved to %s (use --email to send)", filename)

    duration = (datetime.datetime.now() - start).total_seconds()
    logger.info("Step3 DONE: %.1fs", duration)


def daily_run(send_email: bool = True):
    start = datetime.datetime.now()
    logger.info("dailyRun START: %s", start.isoformat())
    tab_name = step1_scrape()
    logger.info("--- Step 1 done, tab: %s ---", tab_name)
    step2_ai(tab_name)
    step3_report(tab_name, send_email=send_email)
    duration = (datetime.datetime.now() - start).total_seconds()
    logger.info("dailyRun DONE: %.1fs", duration)

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    tab  = sys.argv[2] if len(sys.argv) > 2 else None
    if mode == "--step1":
        step1_scrape(tab)
    elif mode == "--step2":
        step2_ai(tab)
    elif mode == "--step3":
        step3_report(tab, send_email="--email" in sys.argv)
    else:
        daily_run(send_email="--email" in sys.argv)
