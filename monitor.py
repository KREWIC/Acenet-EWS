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
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("monitor.log"),
        logging.StreamHandler()
    ]
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


def load_config():
    with open("config.json", "r") as f:
        return json.load(f)


SMS_GATEWAY_DOMAINS = {
    "tmomail.net",
    "vtext.com",
    "txt.att.net",
    "mms.att.net",
    "messaging.sprintpcs.com",
    "vmobl.com",
    "vmobile.com",
    "myboostmobile.com",
    "text.republicwireless.com",
    "msg.fi.google.com",
    "sms.mycricket.com"
}

def is_sms_gateway_address(address):
    parts = address.split("@")
    if len(parts) != 2:
        return False
    return parts[1].lower().strip() in SMS_GATEWAY_DOMAINS


def send_alert(cfg, subject, body):
    """Send email alert to configured recipients, optionally filtering SMS gateways."""
    notif = cfg["notifications"]
    recipients = list(notif["recipients"])
    if not notif.get("send_sms_alerts", True):
        original = recipients
        recipients = [r for r in recipients if not is_sms_gateway_address(r)]
        if len(recipients) != len(original):
            log.info("SMS recipients removed from notification list")

    if not recipients:
        log.warning("No recipients left after SMS filtering; alert not sent")
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
    """Log into AceNet and return True if successful."""
    acenet = cfg["acenet"]
    try:
        log.info("Navigating to login page...")
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
            log.error("Login failed — still on login page")
            return False

        log.info("Login successful")
        return True

    except PlaywrightTimeout:
        log.error("Login timed out")
        return False
    except Exception as e:
        log.error(f"Login error: {e}")
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

        hits = []
        page_skus = []

        for card in cards:
            sku_text = extract_sku_from_card(card)
            if sku_text:
                page_skus.append(sku_text)

            text = card.inner_text()
            html = card.inner_html()

            has_new = "new-icon.svg" in html
            has_on_order = "onordergreen" in html

            if not (has_new or has_on_order):
                continue

            # Extract product name
            name_element = card.query_selector('[class*="name"], [class*="title"], h2, h3')
            product_name = name_element.inner_text() if name_element else "Unknown Product"

            tags = []
            if has_new:
                tags.append("NEW")
            if has_on_order:
                tags.append("ON ORDER FOR RSC")

            hits.append({
                "name": product_name.strip(),
                "sku": sku_text.strip(),
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
        lines.append(f"TAGS: {', '.join(h['tags'])}")
        lines.append(f"Product: {h['name']}")
        lines.append(f"[{', '.join(h['tags'])}] {h['name']} {h['sku']}")
        lines.append(f"Login: acenet.aceservices.com")
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
    first_run = True

    while True:
        try:
            now = datetime.now()

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