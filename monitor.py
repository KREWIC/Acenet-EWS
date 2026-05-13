"""
AceNet Pokemon Monitor
Checks AceNet mobile view for NEW and ON ORDER FOR RSC tags
and sends SMS/email alerts immediately when found.
"""

import json
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

# ── Tags we care about ───────────────────────────────────────
ALERT_TAGS = ["NEW", "ON ORDER FOR RSC"]

# ── URLs ─────────────────────────────────────────────────────
SEARCH_URL = 'https://acenet.aceservices.com/search/product?q={{"QueryText":"{term}","FilterQuery":"","TypeaheadField":"","IsRecentSearch":true,"UserId":"{user}"}}'


def load_config():
    with open("config.json", "r") as f:
        return json.load(f)


def send_alert(cfg, subject, body):
    """Send email + SMS alert to all recipients."""
    notif = cfg["notifications"]
    try:
        msg = MIMEMultipart()
        msg["From"] = notif["sender_email"]
        msg["To"] = ", ".join(notif["recipients"])
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(notif["smtp_host"], notif["smtp_port"]) as server:
            server.starttls()
            server.login(notif["sender_email"], notif["sender_password"])
            server.sendmail(
                notif["sender_email"],
                notif["recipients"],
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
        page.goto(acenet["base_url"], timeout=30000)
        page.wait_for_load_state("networkidle", timeout=30000)

        # Fill credentials
        page.fill('input[name="username"], input[type="text"]', acenet["username"])
        page.fill('input[name="password"], input[type="password"]', acenet["password"])
        page.click('button.login-Btn')
        page.wait_for_load_state("networkidle", timeout=30000)

        # Check if login worked
        if "login" in page.url.lower():
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


def search_pokemon(page, cfg):
    acenet = cfg["acenet"]
    url = SEARCH_URL.format(
        term=acenet["search_term"],
        user=acenet["username"]
    )

    try:
        log.info(f"Searching: {url}")
        page.goto(url, timeout=30000)
        page.wait_for_load_state("networkidle", timeout=30000)

        # Scroll to load all results
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(2)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1)

        # Get all product cards
        cards = page.query_selector_all(".product-card, .product-item, [class*='product']")
        log.info(f"Found {len(cards)} product cards")

        hits = []
        future_items = []

        for card in cards:
            text = card.inner_text()
            html = card.inner_html()

            # Check for alert tags
            has_new = "new-icon.svg" in html
            has_on_order = "onordergreen" in html
            has_future = "FUTURE" in text.upper()

            if not (has_new or has_on_order or has_future):
                continue

            # Extract SKU
            sku_element = card.query_selector('[class*="sku"], [id*="sku"]')
            sku_text = sku_element.inner_text() if sku_element else ""
            if not sku_text:
                # Try to find SKU in text
                lines = text.split("\n")
                sku_text = next((l.strip() for l in lines if "SKU" in l.upper()), "Unknown SKU")

            # Extract product name
            name_element = card.query_selector('[class*="name"], [class*="title"], h2, h3')
            product_name = name_element.inner_text() if name_element else "Unknown Product"

            tags = []
            if has_new:
                tags.append("NEW")
            if has_on_order:
                tags.append("ON ORDER FOR RSC")
            if has_future:
                tags.append("FUTURE")

            item = {
                "name": product_name.strip(),
                "sku": sku_text.strip(),
                "tags": tags,
            }

            if has_new or has_on_order:
                hits.append(item)
            elif has_future:
                future_items.append(item)

        return hits, future_items

    except PlaywrightTimeout:
        log.error("Search timed out")
        return None, None
    except Exception as e:
        log.error(f"Search error: {e}")
        traceback.print_exc()
        return None, None

    except PlaywrightTimeout:
        log.error("Search timed out")
        return None  # None = error, [] = no hits
    except Exception as e:
        log.error(f"Search error: {e}")
        return None


def format_alert_message(hits):
    """Format the alert message for SMS/email."""
    lines = [f"🚨 ACENET POKEMON ALERT — {len(hits)} item(s) found!\n"]
    for h in hits:
        lines.append(f"TAGS: {', '.join(h['tags'])}")
        lines.append(f"Product: {h['name']}")
        lines.append(f"{h['sku']}")
        lines.append(f"Login: acenet.acehardware.com")
        lines.append("")
    return "\n".join(lines)


def run():
    cfg = load_config()
    poll_seconds = cfg["monitor"]["poll_interval_minutes"] * 60
    heartbeat_hour = cfg["monitor"]["heartbeat_hour"]

    log.info("AceNet monitor starting...")
    send_alert(cfg, "AceNet Monitor Started", "Monitor is running. You will receive alerts for NEW and ON ORDER FOR RSC Pokemon products.")

    last_heartbeat_day = None
    known_hits = set()  # Track already-alerted SKUs to avoid spam

    while True:
        try:
            # Daily heartbeat
            now = datetime.now()
            if now.hour == heartbeat_hour and now.date() != last_heartbeat_day:
                send_alert(cfg, "AceNet Monitor Heartbeat", f"Monitor is alive and running. Last check: {now.strftime('%Y-%m-%d %H:%M')}")
                last_heartbeat_day = now.date()

            # Run check
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=False)
                context = browser.new_context(
                    user_agent=MOBILE_USER_AGENT,
                    viewport=MOBILE_VIEWPORT,
                    is_mobile=True,
                    has_touch=True
                )
                page = context.new_page()

                if not login(page, cfg):
                    send_alert(cfg, "AceNet Monitor ERROR", "Failed to log in to AceNet. Check credentials in config.json.")
                    browser.close()
                    time.sleep(poll_seconds)
                    continue

                hits, future_items = search_pokemon(page, cfg)
                browser.close()

            if hits is None:
                # Scraping error
                log.warning("Scrape returned error, will retry next cycle")
            elif len(hits) == 0:
                log.info("No alert tags found this cycle")
                known_hits.clear()  # Reset so we re-alert if something comes back
            else:
                # Filter out already-alerted items
                new_hits = [h for h in hits if h["sku"] not in known_hits]
                if new_hits:
                    msg = format_alert_message(new_hits)
                    send_alert(cfg, f"🚨 ACENET POKEMON — {len(new_hits)} NEW ITEM(S)", msg)
                    for h in new_hits:
                        known_hits.add(h["sku"])
                else:
                    log.info("Hits found but already alerted — no new notification")

        except Exception as e:
            err = traceback.format_exc()
            log.error(f"Unexpected error: {err}")
            send_alert(cfg, "AceNet Monitor CRASH", f"Monitor crashed and restarted.\n\nError:\n{err}")

        log.info(f"Sleeping {cfg['monitor']['poll_interval_minutes']} minutes...\n")
        time.sleep(poll_seconds)


if __name__ == "__main__":
    run()
