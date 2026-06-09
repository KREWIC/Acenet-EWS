"""
AceNet Order Test Script
Finds a specific SKU in search results and places 1 unit across all 3 stores.
No monitoring, no filters, no allocation check — pure ordering flow test.
"""

import json
import time
import traceback
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

TEST_SKU = "9122981"
QTY_TO_ORDER = 1
STORES = ["14320", "18771", "18813"]

SEARCH_URL = 'https://acenet.aceservices.com/search/product?q={{"QueryText":"pokemon","FilterQuery":"","TypeaheadField":"","IsRecentSearch":true,"UserId":"{user}"}}'

MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/16.0 Mobile/15E148 Safari/604.1"
)
MOBILE_VIEWPORT = {"width": 390, "height": 844}


def load_config():
    with open("config.json", "r") as f:
        return json.load(f)


def login(page, cfg):
    acenet = cfg["acenet"]
    print("Logging in...")
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
        raise Exception("Login failed — check credentials in config.json")
    print("Login successful")


def navigate_to_search(page, cfg):
    url = SEARCH_URL.format(user=cfg["acenet"]["username"])
    print("Navigating to Pokemon search results...")
    page.goto(url, timeout=60000)
    page.wait_for_load_state("networkidle", timeout=60000)
    time.sleep(3)
    print("Search results loaded")


def switch_store(page, target_store):
    print(f"Switching to store {target_store}...")
    page.locator('button[data-id="storeSelectorList"]').click(timeout=10000)
    page.wait_for_selector('.store-number', timeout=10000)
    time.sleep(1)
    page.locator(f'.store-number:has-text("{target_store}T")').click(timeout=10000)
    page.wait_for_load_state("networkidle", timeout=30000)
    time.sleep(2)
    print(f"Now on store {target_store}")


def open_item_detail(page, context, sku):
    frame = page.frame(name="iframeRetailAppHostContent")
    if not frame:
        raise Exception("Could not find search iframe")

    frame.wait_for_selector(".product-outer", timeout=30000)
    time.sleep(2)

    cards = frame.query_selector_all(".product-outer")
    print(f"Found {len(cards)} cards in search results")

    target_card = None
    for card in cards:
        if sku in card.inner_text():
            target_card = card
            break

    if not target_card:
        raise Exception(f"SKU {sku} not found in search results")

    name_element = target_card.query_selector('[class*="name"], [class*="title"], h2, h3')
    if not name_element:
        raise Exception(f"Could not find clickable name on card for SKU {sku}")

    print(f"Found SKU {sku}, opening item detail...")
    with context.expect_page() as popup_info:
        name_element.click()

    popup = popup_info.value
    popup.wait_for_load_state("networkidle", timeout=30000)
    time.sleep(2)
    print("Item detail popup open")
    return popup


def place_order(popup, qty, sku):
    print(f"Entering qty {qty}...")
    # The popup has its own iframe — search all frames for the qty input
    qty_input = None
    for frame in popup.frames:
        try:
            loc = frame.locator('input.qtyClass').first
            loc.wait_for(timeout=3000)
            qty_input = loc
            print(f"Found qty input in frame: {frame.url}")
            break
        except Exception:
            continue

    if qty_input is None:
        raise Exception("Could not find input.qtyClass in any frame of the popup")

    qty_input.fill(str(qty), timeout=10000)
    time.sleep(1)

    # Use the same frame we found the qty input in
    active_frame = None
    for frame in popup.frames:
        try:
            frame.locator('input[id*="btnExpressCheckout"]').wait_for(timeout=2000)
            active_frame = frame
            break
        except Exception:
            continue
    if active_frame is None:
        active_frame = popup  # fallback to popup root

    print("Clicking Express Checkout...")
    active_frame.locator('input[id*="btnExpressCheckout"]').click(timeout=10000)
    time.sleep(2)

    # "Greater than RSC" dialog
    try:
        active_frame.locator('text=greater than the quantity').wait_for(timeout=4000)
        print("WARNING: qty greater than RSC, cancelling")
        active_frame.locator('text=Cancel').click(timeout=5000)
        return "skipped_exceeds_rsc"
    except PlaywrightTimeout:
        pass

    # Qty validation error
    try:
        active_frame.locator('text=Please enter a quantity').wait_for(timeout=2000)
        print("ERROR: qty input did not register")
        return "error_qty_input"
    except PlaywrightTimeout:
        pass

    # Review & Submit page
    print("Waiting for Review & Submit page...")
    active_frame.locator('text=Review & Submit').wait_for(timeout=15000)
    print("On Review & Submit, clicking Checkout...")
    active_frame.locator('input#btnCheckOut').click(timeout=10000)

    # Confirmation
    print("Waiting for order confirmation...")
    active_frame.locator('text=Your order was successfully submitted').wait_for(timeout=20000)
    print(f"ORDER CONFIRMED for SKU {sku}!")
    return "ordered"


def run_test():
    cfg = load_config()
    results = []

    print(f"\nTest target: SKU {TEST_SKU}, qty {QTY_TO_ORDER}, stores {STORES}\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            user_agent=MOBILE_USER_AGENT,
            viewport=MOBILE_VIEWPORT,
            is_mobile=True,
            has_touch=True
        )
        page = context.new_page()

        login(page, cfg)
        navigate_to_search(page, cfg)

        for i, store in enumerate(STORES):
            print(f"\n{'='*40}")
            print(f"STORE {store} ({i+1} of {len(STORES)})")
            print(f"{'='*40}")

            try:
                if i > 0:
                    switch_store(page, store)
                    navigate_to_search(page, cfg)

                popup = open_item_detail(page, context, TEST_SKU)
                try:
                    status = place_order(popup, QTY_TO_ORDER, TEST_SKU)
                    results.append({"store": store, "status": status})
                finally:
                    try:
                        popup.close()
                    except Exception:
                        pass

            except Exception as e:
                print(f"ERROR for store {store}: {e}")
                traceback.print_exc()
                results.append({"store": store, "status": "error", "reason": str(e)})

        input("\nDone. Press Enter to close the browser...")
        browser.close()

    print("\n" + "="*40)
    print("FINAL RESULTS")
    print("="*40)
    for r in results:
        status = r["status"]
        reason = f" — {r['reason']}" if "reason" in r else ""
        print(f"Store {r['store']}: {status.upper()}{reason}")


if __name__ == "__main__":
    run_test()
