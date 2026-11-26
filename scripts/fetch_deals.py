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
COUPON_TRACK_FILE = "data/tracked_coupon_asins.json"
LLMS_FILE = "llms.txt"


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def extract_int(val):
    while isinstance(val, list):
        val = val[0] if len(val) > 0 else 0
    return val if isinstance(val, (int, float)) else 0


def parse_keepa_coupon(coupon_arr):
    """
    Keepa Product Object 'coupon' field:
    List of 2 ints [value, type_or_flag] or [val]:
      - Negative value: percentage discount (e.g., -40 -> 40% off)
      - Positive value: fixed discount in cents (e.g., 500 -> $5.00 off)
      - None / empty / 0: no active coupon
    """
    if not coupon_arr or not isinstance(coupon_arr, list):
        return None
    raw_val = coupon_arr[0] if len(coupon_arr) > 0 else 0
    if not isinstance(raw_val, (int, float)) or raw_val == 0:
        return None
    if raw_val < 0:
        pct = abs(int(raw_val))
        if 1 <= pct <= 95:
            return {"type": "percent", "value": pct}
    else:
        dollars = round(float(raw_val) / 100.0, 2)
        if dollars > 0:
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
    Checks live coupon status on Keepa for up to 100 ASINs per request.
    Returns dict: { ASIN: {"active": bool, "type": ..., "value": ...} }
    """
    results = {}
    clean_asins = list(dict.fromkeys([a.strip().upper() for a in asin_list if len(a.strip()) == 10]))
    for i in range(0, len(clean_asins), 100):
        batch = clean_asins[i:i + 100]
        url = f"https://api.keepa.com/product?key={KEEPA_API_KEY}&domain={DOMAIN_ID}&asin={','.join(batch)}&stats=0"
        try:
            res = requests.get(url, timeout=30)
            res.raise_for_status()
            products = res.json().get("products", [])
            returned_set = set()
            for prod in products:
                asin = (prod.get("asin") or "").upper()
                if not asin:
                    continue
                returned_set.add(asin)
                parsed = parse_keepa_coupon(prod.get("coupon"))
                if parsed:
                    results[asin] = {
                        "asin": asin,
                        "active": True,
                        "type": parsed["type"],
                        "value": parsed["value"]
                    }
                else:
                    results[asin] = {"asin": asin, "active": False}
            # Any queried ASIN not returned is treated as inactive
            for a in batch:
                if a not in returned_set:
                    results[a] = {"asin": a, "active": False}
        except Exception as e:
            print(f"Keepa product coupon batch warning: {e}")
    return results


def run_radar():
    now_utc = datetime.now(timezone.utc)
    timestamp_str = now_utc.strftime("%Y-%m-%d %H:%M UTC")

    # 1. Fetch general price drops + coupon-specific deals from Keepa
    general_deals = fetch_keepa_deals(has_coupon=False)
    coupon_deals = fetch_keepa_deals(has_coupon=True)

    sent_asins = set(load_json(SENT_FILE, []))
    tracked_coupons = set(load_json(COUPON_TRACK_FILE, []))

    discovered = []
    new_to_send = []
    candidate_coupon_asins = set(tracked_coupons)

    seen_in_run = set()
    for is_coupon_feed, feed in [(True, coupon_deals), (False, general_deals)]:
        for item in feed[:30]:
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

            if is_coupon_feed:
                candidate_coupon_asins.add(asin)

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

    # 2. Verify live coupon details for both new coupon candidates & existing tracked coupon ASINs
    coupon_status_map = fetch_keepa_product_coupons(list(candidate_coupon_asins))
    coupon_updates = list(coupon_status_map.values())

    # Keep only still-active coupon ASINs in local tracking file
    active_tracked = {a for a, info in coupon_status_map.items() if info.get("active")}

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

    print(f"Tespit edilen ürün: {len(discovered)}, Yeni ASIN: {len(new_to_send)}, Kontrol edilen kupon: {len(coupon_updates)}")

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
            server_coupon_asins = resp_json.get("active_coupon_asins", [])
            if isinstance(server_coupon_asins, list):
                active_tracked.update([a.strip().upper() for a in server_coupon_asins if len(a.strip()) == 10])
        except Exception:
            pass

    with open(COUPON_TRACK_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(list(active_tracked))[-500:], f)


if __name__ == "__main__":
    run_radar()
