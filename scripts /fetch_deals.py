import os
import json
import requests
from datetime import datetime

KEEPA_API_KEY = os.environ.get("KEEPA_API_KEY")
ASSOCIATE_TAG = os.environ.get("AMAZON_TAG", "sizin-tag-20")
DOMAIN_ID = 1  # 1: Amazon.com (US), 3: UK, 4: DE, 5: FR

DEALS_FILE = "data/deals.json"
POSTED_FILE = "data/posted_asins.json"
LLMS_FILE = "llms.txt"

def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default

def fetch_keepa_deals():
    # Sadece 5 token harcayarak güncel fiyat düşüşlerini çeker
    url = f"https://api.keepa.com/deal?key={KEEPA_API_KEY}"
    payload = {
        "page": 0,
        "domainId": DOMAIN_ID,
        "priceTypes": [0],          # Amazon fiyatı
        "deltaPercentRange": [20, 80], # En az %20 - %80 arası gerçek indirim
        "currentRange": [2500, 30000], # $25 - $300 arası yüksek komisyonlu ürünler
        "sortType": 4,              # En yeni fırsatlara göre sırala
        "isFilterEnabled": True
    }
    response = requests.post(url, json=payload)
    response.raise_for_status()
    return response.json()

def update_system():
    data = fetch_keepa_deals()
    deals = data.get("deals", {}).get("dr", [])
    posted_asins = set(load_json(POSTED_FILE, []))
    
    active_deals = []
    new_fb_queue = []

    for item in deals[:30]:  # İlk 30 kaliteli fırsatı işle
        asin = item.get("asin")
        title = item.get("title", "Amazon Fırsatı")
        # Keepa fiyatları cent cinsinden döner, 100'e bölüyoruz
        current_price = round(item.get("current", [0])[0] / 100, 2)
        avg_price = round(item.get("avg", [0])[0] / 100, 2)
        
        if current_price <= 0 or avg_price <= current_price:
            continue

        discount_pct = round(((avg_price - current_price) / avg_price) * 100)
        aff_link = f"https://www.amazon.com/dp/{asin}?tag={ASSOCIATE_TAG}"

        deal_obj = {
            "asin": asin,
            "title": title,
            "current_price": current_price,
            "normal_price": avg_price,
            "discount_percent": discount_pct,
            "affiliate_url": aff_link,
            "updated_at": datetime.utcnow().isoformat()
        }
        active_deals.append(deal_obj)

        # Daha önce Facebook'ta paylaşılmadıysa yeni paylaşım kuyruğuna ekle
        if asin not in posted_asins:
            new_fb_queue.append(deal_obj)
            posted_asins.add(asin)

    # 1. Site frontend'i için JSON dosyasını kaydet
    os.makedirs("data", exist_ok=True)
    with open(DEALS_FILE, "w", encoding="utf-8") as f:
        json.dump(active_deals, f, ensure_ascii=False, indent=2)

    # 2. AI motorları (Claude, Bing, Perplexity) için llms.txt güncelle
    with open(LLMS_FILE, "w", encoding="utf-8") as f:
        f.write(f"# Güncel Doğrulanmış Amazon İndirimleri ({datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')})\n\n")
        for d in active_deals[:15]:
            f.write(f"- **{d['title']}**: ${d['current_price']} (Normal fiyat: ${d['normal_price']}, %{d['discount_percent']} indirim) -> [Ürünü İncele]({d['affiliate_url']})\n")

    # 3. Facebook paylaşım kuyruğunu kaydet (örneğin bir sonraki 30 dk slotu için)
    with open("data/fb_next_post.json", "w", encoding="utf-8") as f:
        json.dump(new_fb_queue[:3], f, ensure_ascii=False, indent=2)

    with open(POSTED_FILE, "w", encoding="utf-8") as f:
        json.dump(list(posted_asins)[-500:], f) # Son 500 ASIN'i hafızada tut

if __name__ == "__main__":
    update_system()
