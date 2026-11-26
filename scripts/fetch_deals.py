import os
import json
import requests
from datetime import datetime, timezone

KEEPA_API_KEY = os.environ.get("KEEPA_API_KEY")
WEBHOOK_SECRET = os.environ.get("NABSTASH_WEBHOOK_SECRET")
WEBHOOK_URL = "https://www.nabstash.com/api/keepa-webhook.php"
DOMAIN_ID = 1  # 1: Amazon.com (US)

RADAR_FILE = "data/keepa_radar.json"
SENT_FILE = "data/sent_asins.json"
COUPON_CURSOR_FILE = "data/coupon_cursor.json"
LLMS_FILE = "llms.txt"

# 1 token/min plan = 30 tokens regenerated every 30 min.
# Keepa /deal costs ~5 tokens per call (2 calls = 10 tokens).
# Keepa /product costs 1 token per ASIN -> max 16 ASINs per run = ~26 tokens total!
MAX_COUPON_CHECKS_PER_RUN = 16


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def extract_int(val):
    while isinstance(val, list):
        val = val[0] if len(val) > 0 else 0
    return val if isinstance(val, (int, float)) else 0


def parse_keepa_coupon(coupon_arr, current_price_cents=0):
    """
    Keepa Product Object 'coupon' field:
      - Negative value: percentage discount (e.g., -40 -> 40% off)
      - Positive value: fixed discount in cents (e.g., 500 -> $5.00 off)
      - Validates against current_price_cents so coupon never zeroes out the price.
    """
    if not coupon_arr or not isinstance(coupon_arr, list):
        return None
    raw_val = coupon_arr[0] if len(coupon_arr) > 0 else 0
    if not isinstance(raw_val, (int, float)) or raw_val == 0:
        return None

    if raw_val < 0:
        pct = abs(int(raw_val))
        if 5 <= pct <= 90:
            return {"type": "percent", "value": pct}
    else:
        cents = float(raw_val)
        dollars = round(cents / 100.0, 2)
        if dollars <= 0:
            return None
        # Ensure fixed coupon is less than 80% of item price
        if current_price_cents > 0 and cents >= (current_price_cents * 0.80):
            return None
        return {"type": "fixed", "value": dollars}
    return None


def fetch_keepa_deals(has_coupon=False):
    url = f"https://api.keepa.com/deal?key={KEEPA_API_KEY}"
    payload = {
        "page": 0,
        "domainId": DOMAIN_ID,
        "priceTypes": [0],
        "deltaPercentRange": [15, 85] if has_coupon else [20, 80],
        "currentRange": [1000, 30000] if has_coupon else [2500, 30000],
        "sortType": 4,
        "isFilterEnabled": True
    }
    if has_coupon:
        payload["hasCoupon"] = True

    response = requests.post(url, json=payload, timeout=30)
    response.raise_for_status()
    return response.json().get("deals", {}).get("dr", [])


def fetch_keepa_product_coupons(asin_list):
    """
    Queries at most MAX_COUPON_CHECKS_PER_RUN ASINs in a single Keepa /product call
    to stay strictly within the 1 token/min (30 tokens / 30 min) budget.
    """
    results = {}
    batch = list(dict.fromkeys([a.strip().upper() for a in asin_list if len(a.strip()) == 10]))[:MAX_COUPON_CHECKS_PER_RUN]
    if not batch:
        return results

    url = f"https://api.keepa.com/product?key={KEEPA_API_KEY}&domain={DOMAIN_ID}&asin={','.join(batch)}&stats=0"
    try:
        res = requests.get(url, timeout=30)
        res.raise_for_status()
        data = res.json()
        tokens_left = data.get("tokensLeft", "?")
        print(f"Keepa kalan token bakiyesi: {tokens_left}")

        products = data.get("products", [])
        returned_set = set()
        for prod in products:
            asin = (prod.get("asin") or "").upper()
            if not asin:
                continue
            returned_set.add(asin)

            # Extract latest Amazon/BuyBox price in cents if available
            price_cents = 0
            csv_arr = prod.get("csv") or []
            if len(csv_arr) > 0 and isinstance(csv_arr[0], list) and len(csv_arr[0]) >= 2:
                last_p = csv_arr[0][-1]
                if isinstance(last_p, (int, float)) and last_p > 0:
                    price_cents = last_p

            parsed = parse_keepa_coupon(prod.get("coupon"), price_cents)
            if parsed:
                results[asin] = {
                    "asin": asin,
                    "active": True,
                    "type": parsed["type"],
                    "value": parsed["value"]
                }
            else:
                results[asin] = {"asin": asin, "active": False}

        for a in batch:
            if a not in returned_set:
                results[a] = {"asin": a, "active": False}
    except Exception as e:
        print(f"Keepa product coupon check warning: {e}")
    return results


def run_radar():
    now_utc = datetime.now(timezone.utc)
    timestamp_str = now_utc.strftime("%Y-%m-%d %H:%M UTC")

    # 1. Fetch general deals & coupon deals (~10 tokens total)
    general_deals = fetch_keepa_deals(has_coupon=False)
    coupon_deals = fetch_keepa_deals(has_coupon=True)

    sent_asins = set(load_json(SENT_FILE, []))
    cursor_state = load_json(COUPON_CURSOR_FILE, {"asins": [], "cursor": 0})
    existing_coupon_pool = cursor_state.get("asins", [])
    cursor = int(cursor_state.get("cursor", 0))

    discovered = []
    new_to_send = []
    new_coupon_candidates = []

    seen_in_run = set()
    for is_coupon_feed, feed in [(True, coupon_deals), (False, general_deals)]:
        for item in feed[:25]:
            asin = (item.get("asin") or "").strip().upper()
            if len(asin) != 10 or asin in seen_in_run:
                continue
            seen_in_run.add(asin)

            current_price = extract_int(item.get("current", 0))
            avg_price = extract_int(item.get("avg", 0))
            if current_price <= 0 or avg_price <= current_price:
                continue

            discount_pct = round(((avg_price - current_price) / avg_price) * 100)
            raw_title = (item.get("title") or f"Amazon Deal {asin}").strip()
            short_title = raw_title[:110]

            if is_coupon_feed and asin not in new_coupon_candidates:
                new_coupon_candidates.append(asin)

            entry = {
                "asin": asin,
                "title": short_title,
                "discount_percent": discount_pct,
                "has_coupon_hint": is_coupon_feed,
                "detected_at": timestamp_str
            }
            discovered.append(entry)

            if asin not in sent_asins:
                new_to_send.append(entry)
                sent_asins.add(asin)

    # 2. Pick up to 16 ASINs to check coupons on Keepa:
    #    Half from new Keepa coupon radar, half rotating through existing site coupon catalog
    to_check = new_coupon_candidates[:8]
    if existing_coupon_pool:
        if cursor >= len(existing_coupon_pool):
            cursor = 0
        slice_existing = existing_coupon_pool[cursor:cursor + (MAX_COUPON_CHECKS_PER_RUN - len(to_check))]
        cursor += len(slice_existing)
        for a in slice_existing:
            if a not in to_check:
                to_check.append(a)

    coupon_status_map = fetch_keepa_product_coupons(to_check)
    coupon_updates = list(coupon_status_map.values())

    # 3. Build policy-compliant llms.txt
    llms_lines = [
        "# Nabstash — Curated Amazon Deal Radar",
        f"> Last Updated: {timestamp_str}",
        "> Disclosure: As an Amazon Associate, Nabstash earns from qualifying purchases. Product prices and availability are accurate as of the date/time indicated on each product page and are subject to change.\n",
        "## Live Deal Pages on Nabstash"
    ]
    for d in discovered[:20]:
        llms_lines.append(
            f"- **{d['title']}** (~{d['discount_percent']}% off detected at {timestamp_str}) -> View live price & details: https://www.nabstash.com/go.php?asin={d['asin']}"
        )
    llms_content = "\n".join(llms_lines)

    os.makedirs("data", exist_ok=True)
    with open(RADAR_FILE, "w", encoding="utf-8") as f:
        json.dump(discovered, f, ensure_ascii=False, indent=2)
    with open(LLMS_FILE, "w", encoding="utf-8") as f:
        f.write(llms_content)
    with open(SENT_FILE, "w", encoding="utf-8") as f:
        json.dump(list(sent_asins)[-1500:], f)

    print(f"Tespit edilen ürün: {len(discovered)}, Yeni ASIN: {len(new_to_send)}, Kupon kontrolü: {len(coupon_updates)}")

    # 4. Sync with Nabstash Webhook
    if WEBHOOK_SECRET:
        res = requests.post(
            f"{WEBHOOK_URL}?secret={WEBHOOK_SECRET}",
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Secret": WEBHOOK_SECRET
            },
            json={
                "asins": new_to_send[:20],
                "coupon_updates": coupon_updates,
                "llms_txt": llms_content
            },
            timeout=35
        )
        print("Nabstash Yanıtı:", res.status_code, res.text)
        try:
            resp_json = res.json()
            server_pool = resp_json.get("active_coupon_asins", [])
            if isinstance(server_pool, list) and server_pool:
                existing_coupon_pool = [a.strip().upper() for a in server_pool if len(a.strip()) == 10]
        except Exception:
            pass

    with open(COUPON_CURSOR_FILE, "w", encoding="utf-8") as f:
        json.dump({"asins": existing_coupon_pool, "cursor": cursor}, f)


if __name__ == "__main__":
    run_radar()
