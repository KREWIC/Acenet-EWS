"""
AceNet Pokemon Monitor
Checks AceNet for NEW and ON ORDER FOR RSC tags
and sends SMS/email alerts immediately when found.
Tracks hot/cold cycles so you get alerted when a SKU reopens for ordering.
"""

import json
import os
import random
import re
import logging
import smtplib
import time
import traceback
import urllib.parse
from datetime import datetime, timedelta
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
    handlers=[_log_handler, logging.StreamHandler(stream=__import__('sys').stdout)]
)
__import__('sys').stdout.reconfigure(encoding='utf-8')
log = logging.getLogger(__name__)

# ── Mobile user agent (iPhone 14) ────────────────────────────
MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/16.0 Mobile/15E148 Safari/604.1"
)
MOBILE_VIEWPORT = {"width": 390, "height": 844}

# ── Search URL ───────────────────────────────────────────────
def build_search_url(cfg):
    acenet = cfg["acenet"]
    query = json.dumps({
        "QueryText": acenet["search_term"],
        "FilterQuery": "",
        "TypeaheadField": "",
        "UserId": acenet["username"],
    }, separators=(",", ":"))
    return f"https://acenet.aceservices.com/search/product?q={urllib.parse.quote(query, safe='')}"


SEEN_SKUS_FILE = "seen_skus.json"
ORDERED_SKUS_FILE = "ordered_skus.json"


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


def load_ordered_skus():
    try:
        with open(ORDERED_SKUS_FILE, "r") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def save_ordered_skus(ordered_skus):
    with open(ORDERED_SKUS_FILE, "w") as f:
        json.dump(sorted(ordered_skus), f, indent=2)


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
            page.wait_for_load_state("load", timeout=60000)
            time.sleep(2)

            page.wait_for_selector('input[type="text"]', timeout=15000)
            page.fill('input[type="text"]', acenet["username"])
            page.fill('input[type="password"]', acenet["password"])
            time.sleep(1)
            page.click('button.login-Btn')
            page.wait_for_load_state("load", timeout=60000)
            time.sleep(2)

            # Wait for the store selector — only present when logged in
            try:
                page.wait_for_selector('button[data-id="storeSelectorList"]', timeout=15000)
            except PlaywrightTimeout:
                raise Exception("Store selector not found after login — likely still on login page")

            # Let post-login async activity settle before navigating away
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeout:
                pass
            time.sleep(5)

            log.info("Login successful")
            return True

        except Exception as e:
            log.warning(f"Login attempt {attempt + 1} failed: {e}")
            try:
                with open(f"login_fail_dump_{attempt + 1}.html", "w", encoding="utf-8") as f:
                    f.write(page.content())
                log.info(f"Saved login_fail_dump_{attempt + 1}.html for inspection")
            except Exception:
                pass
            if attempt < 2:
                log.info("Waiting 30 seconds before retry...")
                time.sleep(30)

    log.error("All login attempts failed")
    return False


def extract_sku_from_card(card):
    sku_element = card.query_selector('[class*="sku"], [id*="sku"], [data-sku]')
    sku_text = sku_element.inner_text().splitlines()[0].strip() if sku_element else ""
    if not sku_text:
        lines = [line.strip() for line in card.inner_text().splitlines()]
        sku_text = next((line for line in lines if line.upper().startswith("SKU") or "SKU:" in line.upper()), "")

    if ":" in sku_text:
        sku_text = sku_text.split(":", 1)[-1].strip()
    if sku_text.upper().startswith("SKU"):
        sku_text = sku_text[3:].strip()

    return sku_text or "Unknown SKU"


def search_pokemon(page, cfg):
    """Search for Pokemon trading cards and return non-cancelled candidates plus all visible SKUs."""
    url = build_search_url(cfg)

    try:
        log.info(f"Searching: {url}")
        for nav_attempt in range(2):
            try:
                with page.expect_navigation(wait_until="load", timeout=120000):
                    page.evaluate(f"window.location.assign({json.dumps(url)})")
                time.sleep(5)
                break
            except Exception as nav_err:
                if nav_attempt == 0:
                    log.warning(f"Search navigation failed ({nav_err}), re-establishing session and retrying...")
                    time.sleep(10)
                    try:
                        with page.expect_navigation(wait_until="load", timeout=30000):
                            page.evaluate(f"window.location.assign({json.dumps(cfg['acenet']['base_url'])})")
                        time.sleep(5)
                    except Exception:
                        pass
                else:
                    raise

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

        candidates = []
        page_skus = []

        for card in cards:
            name_element = card.query_selector('[class*="name"], [class*="title"], h2, h3')
            product_name = name_element.inner_text().strip() if name_element else ""

            name_lower = product_name.lower()
            if "pokemon" not in name_lower or "trading cards" not in name_lower:
                continue

            sku_text = extract_sku_from_card(card)
            if not sku_text or sku_text == "Unknown SKU":
                log.warning(f"Could not extract SKU for '{product_name}', skipping card")
                continue
            page_skus.append(sku_text)

            status_el = card.query_selector('.status-badge')
            status_text = status_el.inner_text().strip().upper() if status_el else ""
            if "CANCELED" in status_text:
                log.info(f"Skipping cancelled: {product_name} ({sku_text})")
                continue

            candidates.append({"name": product_name, "sku": sku_text})

        return candidates, page_skus

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


def format_startup_message(candidates):
    lines = ["ACENET MONITOR STARTED\n"]
    lines.append(f"Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
    if candidates:
        lines.append(f"CANDIDATES TO MONITOR ({len(candidates)}):")
        for c in candidates:
            lines.append(f"  {c['name']} ({c['sku']})")
    else:
        lines.append("No eligible candidates found at startup.")
    return "\n".join(lines)


# ── Auto-ordering ─────────────────────────────────────────────

def switch_store(page, target_store):
    log.info(f"Switching to store {target_store}...")
    try:
        page.locator('button[data-id="storeSelectorList"]').click(timeout=10000)
        page.wait_for_selector('.store-number', timeout=10000)
        time.sleep(1)
        page.locator(f'.store-number:has-text("{target_store}T")').click(timeout=10000)
        page.wait_for_load_state("networkidle", timeout=30000)
        time.sleep(2)
        log.info(f"Switched to store {target_store}")
        return True
    except Exception as e:
        log.error(f"Store switch to {target_store} failed: {e}")
        return False


def open_item_detail(page, context, sku):
    try:
        frame = page.frame(name="iframeRetailAppHostContent")
        if not frame:
            log.error("Could not find search iframe for ordering")
            return None

        cards = frame.query_selector_all(".product-outer")
        target_card = None
        for card in cards:
            if sku in card.inner_text():
                target_card = card
                break

        if not target_card:
            log.warning(f"Could not find card for SKU {sku} in search results")
            return None

        name_element = target_card.query_selector('[class*="name"], [class*="title"], h2, h3')
        if not name_element:
            log.error(f"Could not find clickable name element for SKU {sku}")
            return None

        with context.expect_page() as popup_info:
            name_element.click()

        popup = popup_info.value
        popup.wait_for_load_state("networkidle", timeout=30000)
        time.sleep(2)
        log.info(f"Opened item detail for SKU {sku}")
        return popup

    except Exception as e:
        log.error(f"Failed to open item detail for SKU {sku}: {e}")
        return None


def get_remaining_qty(popup):
    try:
        frame = get_popup_frame(popup)
        text = frame.inner_text('body')
        match = re.search(r'Allocation QTY/Remaining QTY\s*:\s*(\d+)/(\d+)', text, re.IGNORECASE)
        if match:
            allocated = int(match.group(1))
            remaining = int(match.group(2))
            log.info(f"Allocation: {allocated} total, {remaining} remaining")
            return remaining
        backorder_match = re.search(r'Stock Reserve Backorder\s*:\s*(\d+)', text, re.IGNORECASE)
        if backorder_match:
            qty = int(backorder_match.group(1))
            log.info(f"Stock Reserve Backorder qty: {qty}")
            return qty
        log.warning("Could not find qty on item detail page")
        return None
    except Exception as e:
        log.error(f"Failed to read remaining qty: {e}")
        return None


def has_rsc_arrival(popup):
    try:
        frame = get_popup_frame(popup)
        selector = '#ctl00_ctl00_contentMainPlaceHolder_MainContent_shipMethod_lblExpDeliveryText'
        try:
            frame.locator(selector).wait_for(timeout=8000)
            text = frame.locator(selector).inner_text().strip()
            return "Expected Arrival in RSC" in text
        except Exception:
            text = frame.inner_text('body')
            return "Expected Arrival in RSC" in text
    except Exception as e:
        log.error(f"Failed to check RSC arrival text: {e}")
        return False


def get_popup_frame(popup):
    """Find the active content frame within the item detail popup."""
    for frame in popup.frames:
        try:
            frame.locator('input.qtyClass').wait_for(timeout=3000)
            return frame
        except Exception:
            continue
    return popup  # fallback to popup root if no frame found


def attempt_order(popup, qty, sku):
    try:
        frame = get_popup_frame(popup)
        log.info(f"SKU {sku}: using frame {frame.url if hasattr(frame, 'url') else 'root'}")

        frame.locator('input.qtyClass').first.fill(str(qty), timeout=10000)
        time.sleep(1)

        frame.locator('input[id*="btnExpressCheckout"]').click(timeout=10000)
        time.sleep(2)

        # State: "greater than RSC quantity" — nothing available, skip
        try:
            frame.locator('text=greater than the quantity').wait_for(timeout=4000)
            log.info(f"SKU {sku}: qty exceeds RSC availability, skipping")
            frame.locator('text=Cancel').click(timeout=5000)
            return "skipped_exceeds_rsc"
        except PlaywrightTimeout:
            pass

        # State: qty input didn't register
        try:
            frame.locator('text=Please enter a quantity').wait_for(timeout=2000)
            log.warning(f"SKU {sku}: qty input failed")
            return "error_qty_input"
        except PlaywrightTimeout:
            pass

        # Review & Submit page
        frame.locator('text=Review & Submit').wait_for(timeout=15000)
        log.info(f"SKU {sku}: Review & Submit reached, clicking Checkout")
        frame.locator('input#btnCheckOut').click(timeout=10000)

        # Confirmation
        frame.locator('text=Your order was successfully submitted').wait_for(timeout=20000)
        log.info(f"SKU {sku}: order confirmed!")
        return "ordered"

    except PlaywrightTimeout as e:
        log.error(f"Timeout during order for SKU {sku}: {e}")
        return "error_timeout"
    except Exception as e:
        log.error(f"Order attempt failed for SKU {sku}: {e}")
        return "error"


def navigate_to_search(page, cfg):
    url = build_search_url(cfg)
    for nav_attempt in range(2):
        try:
            with page.expect_navigation(wait_until="load", timeout=120000):
                page.evaluate(f"window.location.assign({json.dumps(url)})")
            time.sleep(5)
            return
        except Exception as nav_err:
            if nav_attempt == 0:
                log.warning(f"Search navigation failed ({nav_err}), re-establishing session and retrying...")
                time.sleep(10)
                try:
                    with page.expect_navigation(wait_until="load", timeout=30000):
                        page.evaluate(f"window.location.assign({json.dumps(cfg['acenet']['base_url'])})")
                    time.sleep(5)
                except Exception:
                    pass
            else:
                raise


def place_orders_all_stores(page, context, hit, cfg):
    stores = cfg["auto_order"]["stores"]
    primary_store = stores[0]
    results = []

    for i, store in enumerate(stores):
        log.info(f"--- Ordering {hit['sku']} for store {store} ---")

        if i > 0:
            if not switch_store(page, store):
                results.append({"store": store, "status": "error", "reason": "store switch failed"})
                continue
            navigate_to_search(page, cfg)

        popup = open_item_detail(page, context, hit["sku"])

        if not popup:
            results.append({"store": store, "status": "error", "reason": "could not open item detail"})
            continue

        try:
            remaining = get_remaining_qty(popup)

            if remaining is None:
                log.info(f"SKU {hit['sku']} store {store}: no allocation found, skipping")
                results.append({"store": store, "status": "skipped", "reason": "no allocation available"})
                popup.close()
                continue

            if remaining == 0:
                log.info(f"SKU {hit['sku']} store {store}: 0 remaining, skipping")
                results.append({"store": store, "status": "skipped", "reason": "qty is 0"})
                popup.close()
                continue

            status = attempt_order(popup, remaining, hit["sku"])
            results.append({
                "store": store,
                "status": status,
                "qty": remaining if status == "ordered" else 0
            })

        except Exception as e:
            log.error(f"Unexpected error ordering {hit['sku']} store {store}: {e}")
            results.append({"store": store, "status": "error", "reason": str(e)})
        finally:
            try:
                popup.close()
            except Exception:
                pass

    switch_store(page, primary_store)
    navigate_to_search(page, cfg)
    return results


def format_order_summary(hit, results):
    lines = [f"AUTO-ORDER SUMMARY\n",
             f"Product: {hit['name']}",
             f"SKU: {hit['sku']}\n"]
    for r in results:
        store = r["store"]
        status = r["status"]
        if status == "ordered":
            lines.append(f"Store {store}: ORDERED {r['qty']} units")
        elif status == "skipped":
            lines.append(f"Store {store}: SKIPPED — {r.get('reason', 'no remaining qty')}")
        elif status == "skipped_exceeds_rsc":
            lines.append(f"Store {store}: SKIPPED — nothing available in RSC")
        else:
            lines.append(f"Store {store}: FAILED — {r.get('reason', status)}")
    return "\n".join(lines)


# ── Main loop ──────────────────────────────────────────────────

def run():
    cfg = load_config()
    poll_seconds = cfg["monitor"]["poll_interval_minutes"] * 60
    heartbeat_hour = cfg["monitor"]["heartbeat_hour"]
    auto_order_enabled = cfg.get("auto_order", {}).get("enabled", False)

    log.info("AceNet monitor starting...")

    last_heartbeat_day = None
    seen_skus = load_seen_skus()
    log.info(f"Loaded {len(seen_skus)} previously seen SKUs from disk")
    first_run = True
    consecutive_errors = 0
    ordered_skus = load_ordered_skus()
    if ordered_skus:
        log.info(f"Skipping {len(ordered_skus)} previously ordered SKUs")

    while True:
        try:
            now = datetime.now()

            # Quiet period: 9:00 PM – 4:30 AM
            in_quiet = now.hour >= 21 or now.hour < 4 or (now.hour == 4 and now.minute < 30)
            if in_quiet:
                if now.hour >= 21:
                    wake = now.replace(hour=4, minute=30, second=0, microsecond=0)
                    wake += timedelta(days=1)
                else:
                    wake = now.replace(hour=4, minute=30, second=0, microsecond=0)
                sleep_secs = (wake - now).total_seconds()
                log.info(f"Quiet period active. Sleeping until 4:30 AM ({sleep_secs/3600:.1f}h)...")
                time.sleep(sleep_secs)
                continue

            if now.hour == heartbeat_hour and now.date() != last_heartbeat_day:
                send_alert(cfg, "AceNet Monitor Heartbeat", f"Monitor is alive. Last check: {now.strftime('%Y-%m-%d %H:%M')}")
                last_heartbeat_day = now.date()

            with sync_playwright() as p:
                headless = os.getenv("HEADLESS", "false").lower() == "false"
                browser = p.chromium.launch(
                    headless=headless,
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-blink-features=AutomationControlled",
                    ],
                )
                context = browser.new_context(
                    user_agent=MOBILE_USER_AGENT,
                    viewport=MOBILE_VIEWPORT,
                    is_mobile=True,
                    has_touch=True,
                )
                page = context.new_page()
                # Hide automation signals from bot detection
                page.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )

                if not login(page, cfg):
                    send_alert(cfg, "AceNet Monitor ERROR", "Failed to log in. Check credentials in config.json.")
                    browser.close()
                    time.sleep(poll_seconds)
                    continue

                candidates, page_skus = search_pokemon(page, cfg)

                if candidates is None:
                    log.warning("Scrape returned error, will retry next cycle")
                    send_alert(cfg, "AceNet Monitor Search Failed", "Search scrape failed (login OK but product search errored). Will retry next cycle.")

                else:
                    never_seen = set(page_skus) - seen_skus
                    if never_seen:
                        if first_run:
                            seen_skus.update(never_seen)
                            save_seen_skus(seen_skus)
                            log.info(f"Seeded seen inventory with {len(never_seen)} SKUs")
                        else:
                            sku_list = "\n".join(sorted(never_seen))
                            send_alert(
                                cfg,
                                f"🆕 ACENET — {len(never_seen)} NEW SKU(S) IN CATALOG",
                                f"New SKUs found:\n\n{sku_list}\n\nCheck AceNet for details."
                            )
                            seen_skus.update(never_seen)
                            save_seen_skus(seen_skus)

                    if first_run:
                        msg = format_startup_message(candidates)
                        send_alert(cfg, "AceNet Monitor Started — Candidates Found", msg)
                        first_run = False

                    to_check = [c for c in candidates if c["sku"] not in ordered_skus]
                    log.info(f"Candidates: {len(candidates)} total, {len(to_check)} to check (skipping {len(candidates) - len(to_check)} already ordered)")

                    if auto_order_enabled:
                        for candidate in to_check:
                            popup = open_item_detail(page, context, candidate["sku"])
                            if not popup:
                                continue
                            try:
                                if has_rsc_arrival(popup):
                                    log.info(f"RSC arrival found: {candidate['name']} ({candidate['sku']})")
                                    popup.close()
                                    results = place_orders_all_stores(page, context, candidate, cfg)
                                    ordered_skus.add(candidate["sku"])
                                    save_ordered_skus(ordered_skus)
                                    all_zero = all(r.get("reason") == "qty is 0" for r in results)
                                    if all_zero:
                                        log.info(f"SKU {candidate['sku']}: all stores at 0 allocation, will not recheck")
                                    else:
                                        summary = format_order_summary(candidate, results)
                                        any_success = any(r["status"] == "ordered" for r in results)
                                        subject = (f"🛒 ORDER PLACED: {candidate['name']}" if any_success
                                                   else f"⚠️ ORDER ATTEMPTED: {candidate['name']}")
                                        send_alert(cfg, subject, summary)
                                else:
                                    log.info(f"No RSC arrival: {candidate['sku']}")
                                    popup.close()
                            except Exception:
                                err = traceback.format_exc()
                                log.error(f"Error processing {candidate['sku']}: {err}")
                                send_alert(
                                    cfg,
                                    f"⚠️ AUTO-ORDER FAILED: {candidate['name']}",
                                    f"Auto-ordering failed — please order manually.\n\nSKU: {candidate['sku']}\n\nError:\n{err}"
                                )
                                try:
                                    popup.close()
                                except Exception:
                                    pass
                    else:
                        log.info(f"Auto-order disabled. {len(to_check)} candidates pending.")

                browser.close()

        except Exception:
            err = traceback.format_exc()
            log.error(f"Unexpected error: {err}")
            consecutive_errors += 1
            if consecutive_errors >= 3:
                send_alert(cfg, f"AceNet Monitor CRASH (x{consecutive_errors})", f"Monitor has failed {consecutive_errors} times in a row.\n\nError:\n{err}")
        else:
            consecutive_errors = 0

        jitter = random.uniform(0, 60)
        log.info(f"Sleeping {cfg['monitor']['poll_interval_minutes']} minutes (+{jitter:.0f}s jitter)...\n")
        time.sleep(poll_seconds + jitter)


if __name__ == "__main__":
    run()