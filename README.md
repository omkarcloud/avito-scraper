# 🛒 Avito Scraper

Avito Scraper is a **free and open-source** scraper that gets you **unlimited** detailed Avito data for free.

## ✨ What Can I Get?

- 🚗 **Full details on 200M+ Avito ads** — price, Avito's market estimate, every spec, photos, address & metro
- 🔍 **Search all of Russia** — keyword, category, city, price & every attribute filter Avito offers
- 🏪 **Seller profiles & reviews** — rating breakdown, ad counts, badges & every review with replies
- 🗺️ **Every category & location** — the full 592-node category tree and location IDs for any city

## 🎥 Example: A Full Avito Ad

```json
{
  "id": 7726049894,
  "title": "BMW X5 3.0 AT, 2025, 16 000 км",
  "link": "https://www.avito.ru/moskva/avtomobili/bmw_x5_3.0_at_2025_16_000_km_7726049894",
  "price": { "amount": 12990000, "currency": "RUB", "text": "12 990 000 ₽", "was_lowered": false },
  "market_price": {
    "estimate": 12513800, "low": 12013300, "high": 13014400, "currency": "RUB", "verdict": "Хорошая цена",
    "pros": ["1 собственник — у других таких же авто их больше", "Пробег меньше, чем у 53% аналогичных машин", "ДТП не обнаружено"]
  },
  "params": [
    { "id": 110002, "name": "Год выпуска", "value": "2025" },
    { "id": 2687, "name": "Пробег", "value": "16 000 км" }
  ],
  "options": [{ "name": "Салон", "values": ["Кожа", "Кожаный руль"] }],
  "location": {
    "name": "Москва", "address": "Москва, Киевская ул., 14",
    "coordinates": { "latitude": 55.742727, "longitude": 37.558643 },
    "metro": [{ "name": "Киевская", "distance_meters": 600, "walking_time": "до 5 мин." }]
  },
  "images_count": 33,
  "seller": {
    "name": "АвтоКапитал", "link": "https://www.avito.ru/brands/i13285073", "type": "Компания",
    "on_avito_since": "2012-10", "rating": { "score": 4.4, "reviews_count": 57 }
  },
  "stats": { "views_today": 42, "views_total": null },
  "badges": ["Машина из Селекта", "Цена как в салоне"]
}
```

*Trimmed for readability.*

## 🚀 Unlimited Free Avito Data — Get It in 60 Seconds

1️⃣ Clone and install:
```bash
git clone https://github.com/omkarcloud/avito-scraper
cd avito-scraper
python -m pip install -r requirements.txt
```

2️⃣ Start the API:
```bash
python run.py
```

3️⃣ Get your first data:
```bash
curl "http://localhost:8000/ads/details?ad=7726049894"
```

```json
{
  "id": 7726049894,
  "title": "BMW X5 3.0 AT, 2025, 16 000 км",
  "link": "https://www.avito.ru/moskva/avtomobili/bmw_x5_3.0_at_2025_16_000_km_7726049894",
  "status": { "is_active": true, "is_closed": false, "is_reserved": false, "message": null },
  "price": { "amount": 12990000, "currency": "RUB", "text": "12 990 000 ₽", "was_lowered": false },
  "market_price": { "estimate": 12513800, "low": 12013300, "high": 13014400, "verdict": "Хорошая цена" },
  "location": {
    "id": 637640, "name": "Москва", "address": "Москва, Киевская ул., 14",
    "coordinates": { "latitude": 55.742727, "longitude": 37.558643 }
  },
  "images_count": 33,
  "seller": { "name": "АвтоКапитал", "type": "Компания", "rating": { "score": 4.4, "reviews_count": 57 } },
  "stats": { "views_today": 42, "views_total": null },
  "vehicle_type": "С пробегом",
  "listed_date": "2026-10-25"
}
```

All 7 endpoints are now live at `http://localhost:8000`.

Pulling lots of data? Avito answers busy IPs with a captcha page now and then. The scraper retries it by itself; for steady volume, set `AVITO_PROXY=http://user:pass@host:port` to a rotating Russian residential proxy before `python run.py`.

## 📚 Endpoints

7 endpoints cover everything you need.

| Endpoint | Path | Returns |
|---|---|---|
| Ad Details | `/ads/details` | Everything about one ad: price, market estimate, specs, photos, seller |
| Search Ads | `/ads/search` | 50 ads per page by keyword, category, city, price and filters |
| Seller Details | `/sellers/details` | Profile, rating breakdown, ad counts, badges and addresses |
| Seller Ads | `/sellers/ads` | Every active ad of one seller, 50 per page |
| Seller Reviews | `/sellers/reviews` | Reviews with scores, text, photos and seller replies, 25 per page |
| Search Locations | `/locations/search` | Location IDs for any Russian city or region |
| Category Tree | `/categories/list` | All categories with IDs, slugs and sub-category filters |

## 🔍 Exploring Parameters

The same API is published on RapidAPI, and its playground is the easiest place to try parameters and see raw responses. Once a request looks right, run it locally for **unlimited free** data.

1. [Subscribe to the free plan](https://rapidapi.com/OmkarCloud/api/best-avito-scraper-free-200-calls/pricing) — 200 calls/month, no credit card.
2. [Try the endpoints in the playground](https://rapidapi.com/OmkarCloud/api/best-avito-scraper-free-200-calls/playground) — every param is pre-filled, so you see real data in one click.
3. Copy the generated code and replace `https://best-avito-scraper-free-200-calls.p.rapidapi.com` with `http://localhost:8000`. It will now run against your local API.

```python
import requests

# generated by the playground, host swapped for the local API
response = requests.get(
    "http://localhost:8000/ads/details",
    params={"ad": "7726049894"},
)
print(response.json())
```

## 💬 Have Questions? We Have Answers.

You're a developer — we know how hard completing a project can be. So we offer full support: just message us and we'll reply ✅ with a solution within 1 working day.

[![Message Us on WhatsApp about Avito Scraper](https://raw.githubusercontent.com/omkarcloud/assets/master/images/whatsapp-us.png)](https://www.omkar.cloud/l/avito-scraper-github-whatsapp)

[![Ask Us by Email about Avito Scraper](https://raw.githubusercontent.com/omkarcloud/assets/master/images/ask-on-email.png)](mailto:happy.to.help@omkar.cloud?subject=Help%20with%20Avito%20Scraper%20API&body=I%20need%20help%20using%20the%20Avito%20Scraper%20API.)

## ⚡ Popular Scrapers by Omkar Cloud

- [**Google Maps Scraper (3,100+ GitHub Stars)**](https://github.com/omkarcloud/google-maps-scraper) — type "dentists in New York", get every business as a ready-to-call lead list: phones, emails, websites & reviews. Up to 100K free leads/month.
- [**G2 Scraper**](https://www.omkar.cloud/tools/g2-scraper) — G2 product details, ratings & AI-found contacts
- [**Website Email Contact Scraper**](https://www.omkar.cloud/tools/website-email-contact-scraper) — emails, phones & socials from any website
- [**AliExpress Scraper**](https://www.omkar.cloud/tools/aliexpress-scraper-api) — live product details, SKU variants, stock & shipping
- [**Booking Scraper**](https://www.omkar.cloud/tools/booking-scraper) — Booking.com hotels: prices, ratings, rooms & amenities
- [**Etsy Scraper**](https://www.omkar.cloud/tools/etsy-scraper) — Etsy products: prices, discounts, shops & variations

## ⭐ Love It? [Star It ⭐!](https://github.com/omkarcloud/avito-scraper)

Star the repo ⭐ and become my star hero!

It's just 1 click, but it means the world to me.

[![Star us on GitHub](https://raw.githubusercontent.com/omkarcloud/google-maps-scraper/master/screenshots/star-us.png)](https://github.com/omkarcloud/avito-scraper)
