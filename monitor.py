"""
AceNet Pokemon Monitor
Checks AceNet for NEW and ON ORDER FOR RSC tags
and sends SMS/email alerts immediately when found.
Tracks hot/cold cycles so you get alerted when a SKU reopens for ordering.
"""

import json
import os
import logging
import smtplib
import time
import traceback
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ── Logging ──────────────────────────────────────────────────
from logging.handlers import RotatingFileHandler

_log_handler = RotatingFileHandler("monitor.log", maxBytes=2_000_000, backupCount=3)
_log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[_log_handler, logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ── Mobile user agent (iPhone 14) ────────────────────────────
MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/16.0 Mobile/15E148 Safari/604.1"
)
MOBILE_VIEWPORT = {"width": 390, "height": 844}

# ── Search URL ───────────────────────────────────────────────
SEARCH_URL = 'https://acenet.aceservices.com/search/product?q={{"QueryText":"{term}","FilterQuery":"","TypeaheadField":"","IsRecentSearch":true,"UserId":"{user}"}}'


SEEN_SKUS_FILE = "seen_skus.json"


def load_config():
    with open("config.json", "r") as f:
        return json.load(f)


def load_seen_skus():
    try:
        with open(SEEN_SKUS_FILE, "r") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def save_seen_skus(seen_skus):
    with open(SEEN_SKUS_FILE, "w") as f:
        json.dump(sorted(seen_skus), f, indent=2)


def send_alert(cfg, subject, body):
    notif = cfg["notifications"]
    recipients = list(notif["recipients"])

    if not recipients:
        log.warning("No recipients configured; alert not sent")
        return

    try:
        timestamp = datetime.now().strftime("%m/%d %I:%M%p")
        msg = MIMEMultipart()
        msg["From"] = notif["sender_email"]
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = f"{subject} [{timestamp}]"
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(notif["smtp_host"], notif["smtp_port"]) as server:
            server.starttls()
            server.login(notif["sender_email"], notif["sender_password"])
            server.sendmail(
                notif["sender_email"],
                recipients,
                msg.as_string()
            )
        log.info(f"Alert sent: {subject}")
    except Exception as e:
        log.error(f"Failed to send alert: {e}")


def login(page, cfg):
    acenet = cfg["acenet"]
    for attempt in range(3):
        try:
            log.info(f"Login attempt {attempt + 1}/3...")
            page.goto(acenet["base_url"], timeout=60000)
            page.wait_for_load_state("networkidle", timeout=60000)
            time.sleep(2)

            page.wait_for_selector('input[type="text"]', timeout=15000)
            page.fill('input[type="text"]', acenet["username"])
            page.fill('input[type="password"]', acenet["password"])
            time.sleep(1)
            page.click('button.login-Btn')
            page.wait_for_load_state("networkidle", timeout=60000)
            time.sleep(2)

            if "adfs" in page.url.lower() or "login" in page.url.lower():
                raise Exception("Still on login page after submit")

            log.info("Login successful")
            return True

        except Exception as e:
            log.warning(f"Login attempt {attempt + 1} failed: {e}")
            if attempt < 2:
                log.info("Waiting 30 seconds before retry...")
                time.sleep(30)

    log.error("All login attempts failed")
    return False


def extract_sku_from_card(card):
    sku_element = card.query_selector('[class*="sku"], [id*="sku"], [data-sku]')
    sku_text = sku_element.inner_text().strip() if sku_element else ""
    if not sku_text:
        lines = [line.strip() for line in card.inner_text().splitlines()]
        sku_text = next((line for line in lines if line.upper().startswith("SKU") or "SKU:" in line.upper()), "")

    if ":" in sku_text:
        sku_text = sku_text.split(":", 1)[-1].strip()
    if sku_text.upper().startswith("SKU"):
        sku_text = sku_text[3:].strip()

    return sku_text or "Unknown SKU"


def search_pokemon(page, cfg):
    """Search for pokemon and return list of items with alert tags plus all visible SKUs."""
    acenet = cfg["acenet"]
    url = SEARCH_URL.format(
        term=acenet["search_term"],
        user=acenet["username"]
    )

    try:
        log.info(f"Searching: {url}")
        page.goto(url, timeout=60000)
        page.wait_for_load_state("networkidle", timeout=60000)
        time.sleep(3)

        # Verify we actually landed on the search results page
        if "/search/product" not in page.url:
            log.warning(f"Unexpected redirect after search — landed on: {page.url}. Skipping cycle.")
            return None, []

        # Content is inside an iframe
        frame = page.frame(name="iframeRetailAppHostContent")
        if not frame:
            log.error("Could not find search iframe")
            return None, []

        # Wait for product cards inside the iframe
        try:
            frame.wait_for_selector(".product-outer", timeout=30000)
        except:
            log.warning("Timed out waiting for product cards in iframe")

        # Scroll to load all results
        frame.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(2)
        frame.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1)

        # Get all product cards from iframe
        cards = frame.query_selector_all(".product-outer")
        log.info(f"Found {len(cards)} product cards")

        max_results = cfg["monitor"].get("max_results_sanity", 50)
        if len(cards) > max_results:
            log.warning(f"Result count {len(cards)} exceeds sanity limit of {max_results} — wrong page loaded, skipping cycle.")
            return None, []

        hits = []
        page_skus = []

        for card in cards:
            name_element = card.query_selector('[class*="name"], [class*="title"], h2, h3')
            product_name = name_element.inner_text().strip() if name_element else ""

            if "pokemon" not in product_name.lower():
                continue

            sku_text = extract_sku_from_card(card)
            if not sku_text or sku_text == "Unknown SKU":
                log.warning(f"Could not extract SKU for '{product_name}', skipping card")
                continue
            page_skus.append(sku_text)

            html = card.inner_html()
            has_new = "new-icon.svg" in html
            has_on_order = "onordergreen" in html

            if not (has_new or has_on_order):
                continue

            tags = []
            if has_new:
                tags.append("NEW")
            if has_on_order:
                tags.append("ON ORDER FOR RSC")

            hits.append({
                "name": product_name,
                "sku": sku_text,
                "tags": tags,
            })

        return hits, page_skus

    except PlaywrightTimeout:
        log.error("Search timed out")
        return None, []
    except Exception as e:
        log.error(f"Search error: {e}")
        traceback.print_exc()
        return None, []


def format_alert_message(hits, reopened=False):
    """Format the alert message for SMS/email."""
    if reopened:
        lines = [f"🔁 ACENET REORDER ALERT — {len(hits)} item(s) back in play!\n"]
        lines.append("These SKUs were previously hot, went cold, and are now back:\n")
    else:
        lines = [f"🚨 ACENET POKEMON ALERT — {len(hits)} item(s) found!\n"]
    for h in hits:
        lines.append(f"[{', '.join(h['tags'])}] {h['name']}")
        lines.append(f"SKU: {h['sku']}")
        lines.append("Login: acenet.aceservices.com")
        lines.append("")
    return "\n".join(lines)


def format_startup_message(hits, cold_watch_count=0):
    """Format the startup inventory summary."""
    lines = ["ACENET MONITOR STARTED\n"]
    lines.append(f"Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

    if hits:
        lines.append(f"ITEMS OF INTEREST ({len(hits)}):")
        for h in hits:
            lines.append(f"  [{', '.join(h['tags'])}] {h['name']} {h['sku']}")
    else:
        lines.append("ITEMS OF INTEREST: None found")

    lines.append("")
    lines.append(f"STARTUP COLD WATCH ITEMS: {cold_watch_count}")
    lines.append("These SKUs are being watched for RSC reopenings.")

    return "\n".join(lines)


def run():
    cfg = load_config()
    poll_seconds = cfg["monitor"]["poll_interval_minutes"] * 60
    heartbeat_hour = cfg["monitor"]["heartbeat_hour"]

    log.info("AceNet monitor starting...")

    last_heartbeat_day = None
    known_hits = set()   # SKUs currently hot — don't re-alert
    cold_hits = set()    # SKUs that were hot, went cold — alert if they come back
    seen_skus = load_seen_skus()
    log.info(f"Loaded {len(seen_skus)} previously seen SKUs from disk")
    first_run = True

    quiet_start = cfg["monitor"]["quiet_hours_start"]
    quiet_end = cfg["monitor"]["quiet_hours_end"]

    while True:
        try:
            now = datetime.now()

            # Skip scraping during quiet hours
            if quiet_start <= now.hour or now.hour < quiet_end:
                log.info(f"Quiet hours ({quiet_start}:00-{quiet_end}:00), skipping scrape")
                time.sleep(poll_seconds)
                continue

            # Daily heartbeat
            if now.hour == heartbeat_hour and now.date() != last_heartbeat_day:
                send_alert(cfg, "AceNet Monitor Heartbeat", f"Monitor is alive. Last check: {now.strftime('%Y-%m-%d %H:%M')}")
                last_heartbeat_day = now.date()

            with sync_playwright() as p:
                headless = os.getenv("HEADLESS", "true").lower() == "true"
                browser = p.chromium.launch(headless=headless)
                context = browser.new_context(
                    user_agent=MOBILE_USER_AGENT,
                    viewport=MOBILE_VIEWPORT,
                    is_mobile=True,
                    has_touch=True
                )
                page = context.new_page()

                if not login(page, cfg):
                    send_alert(cfg, "AceNet Monitor ERROR", "Failed to log in. Check credentials in config.json.")
                    browser.close()
                    time.sleep(poll_seconds)
                    continue

                hits, page_skus = search_pokemon(page, cfg)
                browser.close()

            if hits is None:
                log.warning("Scrape returned error, will retry next cycle")

            elif first_run:
                # On startup send full inventory summary and begin cold watch for all visible SKUs.
                for h in hits:
                    known_hits.add(h["sku"])

                startup_cold_items = set(page_skus) - known_hits
                for sku in sorted(startup_cold_items):
                    log.info(f"Startup cold watch SKU: {sku}")
                    cold_hits.add(sku)

                # Silently seed seen_skus on first run — no alert, just baseline
                new_to_seen = set(page_skus) - seen_skus
                if new_to_seen:
                    seen_skus.update(new_to_seen)
                    save_seen_skus(seen_skus)
                    log.info(f"Seeded seen inventory with {len(new_to_seen)} SKUs")

                msg = format_startup_message(hits, cold_watch_count=len(startup_cold_items))
                send_alert(cfg, "AceNet Monitor Started — Items of Interest", msg)
                first_run = False

            else:
                current_skus = {h["sku"] for h in hits}

                # Check for SKUs that just went cold
                newly_cold = known_hits - current_skus
                if newly_cold:
                    for sku in newly_cold:
                        log.info(f"SKU went cold (watching for reorder): {sku}")
                        cold_hits.add(sku)
                    known_hits -= newly_cold

                # Check for brand new hits (never seen before)
                new_hits = [h for h in hits if h["sku"] not in known_hits]

                # Check for reopened hits (were cold, now hot again)
                reopened_hits = [h for h in new_hits if h["sku"] in cold_hits]
                truly_new_hits = [h for h in new_hits if h["sku"] not in cold_hits]

                if reopened_hits:
                    msg = format_alert_message(reopened_hits, reopened=True)
                    send_alert(cfg, f"🔁 ACENET REORDER — {len(reopened_hits)} SKU(S) BACK IN PLAY", msg)
                    for h in reopened_hits:
                        known_hits.add(h["sku"])
                        cold_hits.discard(h["sku"])

                if truly_new_hits:
                    msg = format_alert_message(truly_new_hits)
                    send_alert(cfg, f"🚨 ACENET POKEMON — {len(truly_new_hits)} NEW ITEM(S)", msg)
                    for h in truly_new_hits:
                        known_hits.add(h["sku"])

                # Check for SKUs never seen before in any prior session
                never_seen = set(page_skus) - seen_skus
                if never_seen:
                    sku_list = "\n".join(sorted(never_seen))
                    send_alert(
                        cfg,
                        f"🆕 ACENET — {len(never_seen)} NEW SKU(S) IN CATALOG",
                        f"The following SKU(s) have never been seen before:\n\n{sku_list}\n\nCheck AceNet for details."
                    )
                    seen_skus.update(never_seen)
                    save_seen_skus(seen_skus)
                    log.info(f"Added {len(never_seen)} new SKUs to seen inventory")

                if not new_hits:
                    log.info(f"No new hits this cycle. Hot: {len(known_hits)} Cold: {len(cold_hits)}")

        except Exception as e:
            err = traceback.format_exc()
            log.error(f"Unexpected error: {err}")
            send_alert(cfg, "AceNet Monitor CRASH", f"Monitor crashed and restarted.\n\nError:\n{err}")

        log.info(f"Sleeping {cfg['monitor']['poll_interval_minutes']} minutes...\n")
        time.sleep(poll_seconds)


if __name__ == "__main__":
    run()