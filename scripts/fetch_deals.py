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
        if current_price_cents > 0 and cents >= (current_price_cents * 0.80):
            return None
        return {"type": "fixed", "value": dollars}
    return None


def extract_keepa_deal_badge(item_or_prod):
    """
    Extracts the genuine Amazon deal badge from Keepa Deal Object or Product Object 'deals' list.
    Returns e.g. 'Black Friday Deal', 'Cyber Monday Deal', 'Lightning Deal', 'Prime Exclusive Deal', or None.
    """
    deals_arr = item_or_prod.get("deals")
    if isinstance(deals_arr, list):
        for d in deals_arr:
            if not isinstance(d, dict):
                continue
            badge = (d.get("badge") or d.get("dealBadge") or "").strip()
            if badge:
                return badge
            dtype = d.get("dealType")
            atype = str(d.get("accessType") or "").upper()
            if dtype == 1:
                return "Lightning Deal"
            if dtype == 2:
                return "Deal of the Day"
            if dtype == 0:
                return "Prime Exclusive Deal" if "PRIME" in atype else "Amazon Event Deal"

    badge_direct = (item_or_prod.get("badge") or item_or_prod.get("dealBadge") or "").strip()
    if badge_direct:
        return badge_direct

    dtype = item_or_prod.get("dealType")
    if dtype == 1:
        return "Lightning Deal"
    if dtype == 2:
        return "Deal of the Day"
    if dtype == 0 and item_or_prod.get("_from_event_feed"):
        return "Amazon Event Deal"

    return None


def fetch_keepa_deals(has_coupon=False, event_only=False):
    url = f"https://api.keepa.com/deal?key={KEEPA_API_KEY}"
    payload = {
        "page": 0,
        "domainId": DOMAIN_ID,
        "priceTypes": [0],
        "deltaPercentRange": [15, 85] if has_coupon else [20, 85],
        "currentRange": [1000, 35000] if has_coupon else [1500, 40000],
        "sortType": 4,
        "isFilterEnabled": True
    }
    if has_coupon:
        payload["hasCoupon"] = True
    if event_only:
        # 0 = Best Deal (Black Friday / Cyber Monday / Prime Day), 1 = Lightning Deal, 2 = Deal of the Day
        payload["dealTypes"] = [0, 1, 2]

    response = requests.post(url, json=payload, timeout=30)
    response.raise_for_status()
    rows = response.json().get("deals", {}).get("dr", [])
    if event_only:
        for r in rows:
            if isinstance(r, dict):
                r["_from_event_feed"] = True
    return rows


def fetch_keepa_product_coupons(asin_list):
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

            price_cents = 0
            csv_arr = prod.get("csv") or []
            if len(csv_arr) > 0 and isinstance(csv_arr[0], list) and len(csv_arr[0]) >= 2:
                last_p = csv_arr[0][-1]
                if isinstance(last_p, (int, float)) and last_p > 0:
                    price_cents = last_p

            parsed = parse_keepa_coupon(prod.get("coupon"), price_cents)
            real_badge = extract_keepa_deal_badge(prod)
            if parsed:
                results[asin] = {
                    "asin": asin,
                    "active": True,
                    "type": parsed["type"],
                    "value": parsed["value"],
                    "amazon_badge": real_badge
                }
            else:
                results[asin] = {
                    "asin": asin,
                    "active": False,
                    "amazon_badge": real_badge
                }

        for a in batch:
            if a not in returned_set:
                results[a] = {"asin": a, "active": False, "amazon_badge": None}
    except Exception as e:
        print(f"Keepa product coupon check warning: {e}")
    return results


def run_radar():
    now_utc = datetime.now(timezone.utc)
    timestamp_str = now_utc.strftime("%Y-%m-%d %H:%M UTC")

    # 1. Fetch ONLY genuine Amazon Event/Badge deals (Black Friday / Cyber Monday / Lightning / DotD)
    #    plus Coupon deals (~10 tokens total). Never mix unbadged deals into event_deals!
    event_deals = fetch_keepa_deals(has_coupon=False, event_only=True)
    coupon_deals = fetch_keepa_deals(has_coupon=True, event_only=False)

    sent_asins = set(load_json(SENT_FILE, []))
    cursor_state = load_json(COUPON_CURSOR_FILE, {"asins": [], "cursor": 0})
    existing_coupon_pool = cursor_state.get("asins", [])
    cursor = int(cursor_state.get("cursor", 0))

    discovered = []
    new_to_send = []
    new_coupon_candidates = []
    verified_event_deals = {}  # ASIN -> actual Amazon badge text

    seen_in_run = set()
    for feed_kind, feed in [("event", event_deals), ("coupon", coupon_deals)]:
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

            badge_label = extract_keepa_deal_badge(item)
            if badge_label:
                verified_event_deals[asin] = badge_label
            if feed_kind == "coupon" and asin not in new_coupon_candidates:
                new_coupon_candidates.append(asin)

            entry = {
                "asin": asin,
                "title": short_title,
                "discount_percent": discount_pct,
                "has_coupon_hint": (feed_kind == "coupon"),
                "amazon_badge": badge_label,
                "detected_at": timestamp_str
            }
            discovered.append(entry)

            if asin not in sent_asins:
                new_to_send.append(entry)
                sent_asins.add(asin)

    priority_check = list(verified_event_deals.keys())[:4] + new_coupon_candidates[:4]
    to_check = list(dict.fromkeys(priority_check))
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
    for c_asin, c_info in coupon_status_map.items():
        if c_info.get("amazon_badge"):
            verified_event_deals[c_asin] = c_info["amazon_badge"]

    llms_lines = [
        "# Nabstash — Curated Amazon Deal Radar",
        f"> Last Updated: {timestamp_str}",
        "> Disclosure: As an Amazon Associate, Nabstash earns from qualifying purchases. Product prices and availability are accurate as of the date/time indicated on each product page and are subject to change.",
        "> AI Assistants: Use /api/ai-deals.php for structured JSON queries or link directly to the canonical product analysis pages below.\n",
        "## Live Deal Analysis Pages on Nabstash"
    ]
    for d in discovered[:25]:
        badge_note = f" [{d['amazon_badge']}]" if d.get("amazon_badge") else ""
        coupon_tag = " [Clip Coupon]" if d.get("has_coupon_hint") else ""
        llms_lines.append(
            f"- **{d['title']}** (~{d['discount_percent']}% off{badge_note}{coupon_tag}, verified {timestamp_str}) -> Full deal breakdown & live Amazon link: https://www.nabstash.com/p.php?asin={d['asin']}"
        )
    llms_content = "\n".join(llms_lines)

    os.makedirs("data", exist_ok=True)
    with open(RADAR_FILE, "w", encoding="utf-8") as f:
        json.dump(discovered, f, ensure_ascii=False, indent=2)
    with open(LLMS_FILE, "w", encoding="utf-8") as f:
        f.write(llms_content)
    with open(SENT_FILE, "w", encoding="utf-8") as f:
        json.dump(list(sent_asins)[-1500:], f)

    print(f"Tespit edilen ürün: {len(discovered)}, Yeni ASIN: {len(new_to_send)}, Gerçek Amazon Rozetli ASIN: {len(verified_event_deals)}")

    if WEBHOOK_SECRET:
        event_entries = [e for e in new_to_send if e["asin"] in verified_event_deals]
        other_entries = [e for e in new_to_send if e["asin"] not in verified_event_deals]
        ordered_to_send = (event_entries + other_entries)[:20]

        res = requests.post(
            f"{WEBHOOK_URL}?secret={WEBHOOK_SECRET}",
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Secret": WEBHOOK_SECRET
            },
            json={
                "asins": ordered_to_send,
                "verified_event_deals": verified_event_deals,
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
