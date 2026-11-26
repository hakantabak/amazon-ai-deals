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
LLMS_FILE = "llms.txt"

def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default

def fetch_keepa_deal_asins():
    # Sadece 5 token harcayarak anlık indirimdeki ürünlerin ASIN radarını çeker
    url = f"https://api.keepa.com/deal?key={KEEPA_API_KEY}"
    payload = {
        "page": 0,
        "domainId": DOMAIN_ID,
        "priceTypes": [0],             # Amazon ana fiyatı
        "deltaPercentRange": [20, 80], # %20 - %80 arası gerçek indirimler
        "currentRange": [2500, 30000], # $25 - $300 arası yüksek sepet tutarlı ürünler
        "sortType": 4,                 # En yeni tespit edilen fırsatlar
        "isFilterEnabled": True
    }
    response = requests.post(url, json=payload, timeout=30)
    response.raise_for_status()
    return response.json()

def run_radar():
    now_utc = datetime.now(timezone.utc)
    timestamp_str = now_utc.strftime("%Y-%m-%d %H:%M UTC")
    
    data = fetch_keepa_deal_asins()
    deals = data.get("deals", {}).get("dr", [])
    sent_asins = set(load_json(SENT_FILE, []))
    
    discovered = []
    new_to_send = []

    for item in deals[:30]:
        asin = item.get("asin")
        if not asin:
            continue

        current_price = item.get("current", [0])[0]
        avg_price = item.get("avg", [0])[0]
        if current_price <= 0 or avg_price <= current_price:
            continue

        discount_pct = round(((avg_price - current_price) / avg_price) * 100)
        raw_title = (item.get("title") or f"Amazon Deal {asin}").strip()
        short_title = raw_title[:110]

        entry = {
            "asin": asin,
            "title": short_title,
            "discount_percent": discount_pct,
            "detected_at": timestamp_str
        }
        discovered.append(entry)

        if asin not in sent_asins:
            new_to_send.append(entry)
            sent_asins.add(asin)

    # Amazon Associates Politikasına %100 uyumlu llms.txt oluştur
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
        json.dump(list(sent_asins)[-1000:], f)

    # Yeni tespit edilen ASIN'leri nabstash.com kuyruğuna ilet
    if WEBHOOK_SECRET and (new_to_send or discovered):
        res = requests.post(
            WEBHOOK_URL,
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Secret": WEBHOOK_SECRET
            },
            json={
                "asins": new_to_send[:15],
                "llms_txt": llms_content
            },
            timeout=30
        )
        print("Nabstash Yanıtı:", res.status_code, res.text)

if __name__ == "__main__":
    run_radar()
