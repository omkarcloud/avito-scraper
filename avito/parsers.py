"""Raw avito.ru pages / JSON -> clean dicts. Pure functions, no I/O.

Fixtures (avito/fixtures/, gzipped) are real responses captured from a
Russian residential exit on 2026-10-24 (plus the proof-of-work page from
2026-10-23):

  search_*.json.gz         /web/1/js/items (cars, flats page 2, vacancies,
                           a filtered Toyota search, a no-match query, the
                           services root category's "vertical" landing)
  item_*.html.gz           ad pages: car (dealer, market estimate, options),
                           long-term rental flat (house / rent terms / rules,
                           Rosreestr check), vacancy (job posting), phone,
                           a closed ad, the /not-found page an unknown id
                           redirects to (it also carries the category tree)
  seller_*.html.gz         public profiles: a shop, a person (/brands/i<id>),
                           a private profile by key (/user/<key>/profile),
                           an unknown profile, and a real profile served
                           without its state (the site's degraded render)
  seller_ads_page*.json.gz /web/1/profile/items pages 1 (50) and 2 (29)
  seller_reviews_*.json.gz /web/7/user/<key>/ratings (newest page 2, negative first)
  locations_*.json.gz      /web/1/slocations
  location_page_kazan      /kazan (read for its location id)
  challenge_*.html.gz      the 439 proof-of-work page and the 429 IP captcha

Every parser tolerates missing keys (.get with defaults, type checks) and
raises ParseError only when the payload is not the page it claims to be.
"""
import html as html_lib
import json
import fastjson
import math
import re
from datetime import datetime, timedelta, timezone

from avito import refs
from scraper_errors import UpstreamError
from scraper_utils.values import as_dict as _dict, as_list as _list
from scraper_utils.text import clean as _clean


class ParseError(UpstreamError):
    """The payload is not the page/JSON the parser expects (layout change):
    a 502, never a 400 (route_glue maps ValueError to 400)."""


MOSCOW = timezone(timedelta(hours=3))   # no DST since 2014

_HYDRATION_RE = re.compile(r'window\.__staticRouterHydrationData = JSON\.parse\("((?:[^"\\]|\\.)*)"\)', re.S)
_PRELOADED_RE = re.compile(r'window\.__preloadedState__ = "((?:[^"\\]|\\.)*)"', re.S)
_MFE_RE = re.compile(r'<script type="mime/invalid" data-mfe-state="true">(.*?)</script>', re.S)
_SIZE_KEY_RE = re.compile(r"^(\d+)x(\d+)$")
_INT_RE = re.compile(r"\d[\d\s  ]*")
_TAG_RE = re.compile(r"<[^>]+>")

MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7,
          "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
          "январь": 1, "февраль": 2, "март": 3, "апрель": 4, "май": 5, "июнь": 6, "июль": 7,
          "август": 8, "сентябрь": 9, "октябрь": 10, "ноябрь": 11, "декабрь": 12}


# ---- small value helpers ------------------------------------------------------------------


def _int(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    match = _INT_RE.search(str(value))
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(0))
    return int(digits) if digits else None


def _float(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError):
        return None


def html_to_text(value):
    """The site's description HTML (<p>, <br>, <ul><li>) -> plain text with
    line breaks kept."""
    if not value:
        return None
    text = str(value)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>|</li\s*>|</h\d\s*>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "- ", text)
    text = _TAG_RE.sub("", text)
    text = html_lib.unescape(text).replace(" ", " ").replace(" ", " ")
    lines = [" ".join(line.split()) for line in text.split("\n")]
    text = "\n".join(lines).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text or None


def iso_from_ms(value):
    ms = _int(value)
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_from_seconds(value):
    seconds = _int(value)
    if not seconds:
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ru_date(text, now=None):
    """The site's Moscow-time date text -> ISO. With a time of day:
    'сегодня в 11:42', 'вчера в 23:41', '17 октября в 20:14' ->
    'YYYY-MM-DDTHH:MM:00Z' (UTC). A bare day: '15 сентября 2023', '7 мая',
    '2 июля 2024' -> 'YYYY-MM-DD'. A day without a year is the latest such
    day not after `now`. None when the text is not a date."""
    text = _clean(text)
    if not text:
        return None
    now = (now or datetime.now(timezone.utc)).astimezone(MOSCOW)
    low = text.lower()
    hm = re.search(r"(\d{1,2}):(\d{2})", low)
    day = None
    if low.startswith("сегодня"):
        day = now.date()
    elif low.startswith("вчера"):
        day = (now - timedelta(days=1)).date()
    else:
        match = re.search(r"(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?", low)
        if not match or match.group(2) not in MONTHS:
            return None
        month, dom = MONTHS[match.group(2)], int(match.group(1))
        year = int(match.group(3)) if match.group(3) else now.year
        try:
            day = datetime(year, month, dom).date()
        except ValueError:
            return None
        if not match.group(3) and day > now.date():
            day = day.replace(year=year - 1)
    if hm:
        local = datetime(day.year, day.month, day.day, int(hm.group(1)), int(hm.group(2)), tzinfo=MOSCOW)
        return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return day.isoformat()


def parse_ru_day(text, now=None):
    """The day part of the site's date text: '17 октября в 20:14' ->
    '2026-10-17', 'вчера в 23:41' -> yesterday. An ad page renders its date
    in the VISITOR's regional time (the same ad read 18:41 from Moscow,
    21:41 from Omsk, 23:41 from Ulan-Ude exits, 2026-10-26) and carries no
    epoch, so only the day is reported, read in Moscow time."""
    value = parse_ru_date(text, now)
    return value[:10] if value else None


def parse_month_year(text):
    """'октября 2012' / 'январь 2023' (seller tenure) -> '2012-10'."""
    text = _clean(text)
    if not text:
        return None
    match = re.search(r"([а-яё]+)\s+(\d{4})", text.lower())
    if not match or match.group(1) not in MONTHS:
        return None
    return f"{match.group(2)}-{MONTHS[match.group(1)]:02d}"


def distance_meters(text):
    """' 700 м' -> 700, ' 1,8 км' -> 1800."""
    text = _clean(text)
    if not text:
        return None
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(км|м)\b", text)
    if not match:
        return None
    number = float(match.group(1).replace(",", "."))
    return int(round(number * 1000)) if match.group(2) == "км" else int(round(number))


def largest_image(sizes):
    """{'208x208': url, '636x636': url, ...} -> the URL of the largest size."""
    if isinstance(sizes, str):
        return sizes
    best, best_area = None, -1
    for key, url in _dict(sizes).items():
        match = _SIZE_KEY_RE.match(str(key))
        if match and isinstance(url, str):
            area = int(match.group(1)) * int(match.group(2))
            if area > best_area:
                best, best_area = url, area
    if best is None:
        urls = [u for u in _dict(sizes).values() if isinstance(u, str) and u.startswith("http")]
        best = urls[0] if urls else None
    return best


def _badge_titles(badges):
    out = []
    for badge in _list(badges):
        title = _clean(_dict(badge).get("title"))
        if title and title not in out:
            out.append(title)
    return out


def _metro(references):
    out = []
    for ref in _list(references):
        ref = _dict(ref)
        name = _clean(ref.get("content"))
        if not name:
            continue
        out.append({
            "name": name,
            "distance_meters": distance_meters(ref.get("after")),
            "walking_time": _clean(_dict(ref.get("afterWithIcon")).get("text")),
            "line_colors": [c for c in _list(ref.get("colors")) if isinstance(c, str)] or None,
        })
    return out


def _filters_string(params):
    """The site's {"5": 18, "598": 6396} sub-category params -> the
    `filters` input that reproduces them ("5:18,598:6396")."""
    pairs = []
    for k, v in _dict(params).items():
        if not str(k).isdigit() or v is None:
            continue
        if isinstance(v, list):   # multi-value ({"499": [5254]}) -> the "|" form parse_filters takes
            v = "|".join(str(x) for x in v if x is not None)
            if not v:
                continue
        pairs.append(f"{k}:{v}")
    return ",".join(pairs) or None


# ---- page state extractors ----------------------------------------------------------------

def hydration_data(page):
    match = _HYDRATION_RE.search(page or "")
    if not match:
        return None
    return fastjson.loads(fastjson.loads('"' + match.group(1) + '"'))


def preloaded_state(page):
    match = _PRELOADED_RE.search(page or "")
    if not match:
        return None
    return fastjson.loads(fastjson.loads('"' + match.group(1) + '"'))


def mfe_state(page):
    match = _MFE_RE.search(page or "")
    if not match:
        return None
    return fastjson.loads(match.group(1))


def is_challenge_page(page):
    """The 439 proof-of-work or the 429 IP captcha page."""
    head = (page or "")[:6000]
    return "Доступ ограничен" in head and ("проверка безопасности" in head or "проблема с IP" in head)


# ---- search cards (/web/1/js/items, /web/1/profile/items) ---------------------------------

def _step_payloads(iva, name):
    return [(_dict(_dict(s).get("componentData")).get("component"), _dict(_dict(s).get("payload")))
            for s in _list(_dict(iva).get(name))]


def _card_price(item):
    detailed = _dict(item.get("priceDetailed"))
    shown = None
    monthly = None
    has_discount_icon = False
    has_delivery = False
    sale = {}
    for component, payload in _step_payloads(item.get("iva"), "PriceStep"):
        if component == "price":
            shown = _dict(payload.get("priceDetailed")) or shown
        elif component == "auto-monthly-payment":
            monthly = _int(payload.get("value"))
        elif component in ("discount-icon", "discount", "saleIcon"):
            has_discount_icon = True
            if component == "discount":
                # a sale: the card shows the sale price, priceDetailed keeps
                # the pre-sale one ({"old_string": "52 000", "percent": 5})
                sale = payload
        elif component == "delivery":
            has_delivery = True
    shown = shown or detailed
    amount = _int(shown.get("value")) if shown.get("value") not in (None, 0) or detailed.get("hasValue") else None
    full = detailed.get("fullString") or ""
    shown_str, detailed_str = shown.get("string"), detailed.get("string")
    if shown_str and detailed_str and shown_str != detailed_str:
        # fullString spells priceDetailed; the text follows the shown price
        full = full.replace(detailed_str, shown_str, 1) if detailed_str in full else f"{shown_str} ₽"
    text = _clean(full) or _clean(shown.get("string"))
    if amount is None and not text:
        return None, has_delivery
    lowest = _int(detailed.get("value"))
    # dealer cars: "от 5 950 000 ₽" printed, value = stringWithoutDiscount
    # ("6 750 000 ₽ без скидки"), priceDetailed.value = the discounted price
    has_discount = has_discount_icon or bool(shown.get("stringWithoutDiscount"))
    old = (_int(item.get("priceWithoutDiscount")) or _int(shown.get("stringWithoutDiscount"))
           or _int(sale.get("old_string")))
    price = {
        "amount": amount,
        "currency": refs.CURRENCY if amount is not None else None,
        "text": text,
        "unit": _clean(shown.get("postfix")) or _clean(detailed.get("postfix")),
        "discounted_amount": lowest if (has_discount and lowest and amount and lowest < amount) else None,
        "old_amount": old if old != amount else None,
        "discount_percent": _int(item.get("discountPercent")) or _int(sale.get("percent")),
        "monthly_payment": monthly,
        "was_lowered": bool(shown.get("wasLowered") or detailed.get("wasLowered")),
    }
    return price, has_delivery


def _card_seller(item):
    iva = _dict(item.get("iva"))
    name = link = None
    rating = None
    badges = []
    for component, payload in _step_payloads(iva, "UserInfoStep"):
        if component == "seller-info":
            profile = _dict(payload.get("profile"))
            name = _clean(profile.get("title")) or name
            link = profile.get("link") or link
            r = _dict(payload.get("rating"))
            if r:
                rating = {"score": _float(r.get("score")), "reviews_count": _int(r.get("summary"))}
        elif component == "seller-badgebar":
            badges += _badge_titles(payload.get("badges"))
    logo = _dict(item.get("userLogo"))
    link = link or logo.get("link")
    if not name:
        for component, payload in _step_payloads(iva, "FourthLineStep"):
            if component == "link":
                name = _clean(payload.get("value")) or name
                link = link or payload.get("link") or payload.get("url")
    slug = None
    if link:
        match = re.search(r"/brands/([^/?#]+)", str(link))
        slug = match.group(1) if match else None
    closed = _int(item.get("closedItemsText"))
    if not (name or slug or rating):
        return None
    return {
        "id": slug,
        "name": name,
        "link": refs.seller_link(slug) if slug else None,
        "logo": logo.get("src") if isinstance(logo.get("src"), str) else None,
        "rating": rating,
        "closed_ads_count": closed,
        "badges": badges,
    }


def _card_labels(iva):
    """The card's short text lines (seller kind "Автодилер" / "Компания",
    a vacancy's line of business, a service's sub-category)."""
    out = []
    for step in ("FirstLineStep", "SecondLineStep", "ThirdLineStep"):
        for component, payload in _step_payloads(iva, step):
            value = _clean(payload.get("value"))
            if value and value not in out:
                out.append(value)
    return out


def parse_card(item):
    """One catalog card -> a clean ad summary."""
    item = _dict(item)
    iva = _dict(item.get("iva"))
    price, price_delivery = _card_price(item)
    category = _dict(item.get("category"))
    location = _dict(item.get("location"))
    coords = _dict(item.get("coords"))
    geo = _dict(item.get("geo"))
    geo_step = {}
    for step in ("GeoStep", "DevelopmentNameStep"):
        for component, payload in _step_payloads(iva, step):
            if component == "geo":
                geo_step = _dict(payload.get("geoForItems")) or geo_step
    address = (_clean(coords.get("address_user")) or _clean(geo.get("formattedAddress"))
               or _clean(geo_step.get("formattedAddress")) or _clean(_dict(item.get("userLogo")).get("imageAlt")))
    images = [largest_image(i) for i in _list(item.get("images"))]
    images = [i for i in images if i]
    video = _dict(item.get("nativeVideo"))
    contacts = _dict(item.get("contacts"))
    params_summary = None
    for component, payload in _step_payloads(iva, "AutoParamsStep"):
        params_summary = _clean(payload.get("text")) or params_summary
    for step in ("SparePartsParamsStep",):
        for component, payload in _step_payloads(iva, step):
            params_summary = params_summary or _clean(payload.get("text") or payload.get("value"))
    price_list = []
    for component, payload in _step_payloads(iva, "PriceListStep"):
        for row in _list(payload.get("valuesAll") or payload.get("values")):
            row = _dict(row)
            if _clean(row.get("title")):
                price_list.append({"service": _clean(row.get("title")), "price": _clean(row.get("price"))})
    badges = []
    for component, payload in _step_payloads(iva, "BadgeBarStep"):
        badges += _badge_titles(payload.get("badges"))
    promotions = []
    for component, payload in _step_payloads(iva, "DateInfoStep"):
        for vas in _list(payload.get("vas")):
            title = _clean(_dict(vas).get("title"))
            if title and title not in promotions:
                promotions.append(title)
    lat, lng = _float(coords.get("lat")), _float(coords.get("lng"))
    return {
        "id": _int(item.get("id")),
        "title": _clean(item.get("title")),
        "link": refs.ad_link(item.get("urlPath")),
        "description": html_to_text(item.get("description")),
        "price": price,
        "params_summary": params_summary,
        "price_list": price_list,
        "category": {
            "id": _int(category.get("id")) or _int(item.get("categoryId")),
            "name": _clean(category.get("name")),
            "slug": _clean(category.get("slug")),
            "root_id": _int(category.get("rootId")),
        } if (category or item.get("categoryId")) else None,
        "location": {
            "id": _int(location.get("id")) or _int(item.get("locationId")),
            "name": _clean(location.get("name")) or _clean(geo_step.get("addressLocality")),
            "address": address,
            "coordinates": {"latitude": lat, "longitude": lng} if lat is not None and lng is not None else None,
            "metro": _metro(geo.get("geoReferences") or geo_step.get("geoReferences")),
        },
        "images": images,
        "images_count": _int(item.get("imagesCount")) if item.get("imagesCount") is not None else len(images),
        "has_video": bool(item.get("hasVideo") or video.get("videoUrl")),
        "video_link": video.get("videoUrl") if isinstance(video.get("videoUrl"), str) else None,
        "seller": _card_seller(item),
        "labels": _card_labels(iva),
        "badges": badges,
        "promotions": promotions,
        "is_verified": bool(item.get("isVerifiedItem")),
        "is_reserved": bool(item.get("isReserved")),
        "is_delivery_available": bool(contacts.get("delivery") or price_delivery),
        "is_marketplace": bool(item.get("isMarketplace")),
        "has_phone": bool(contacts.get("phone")) if contacts else None,
        "listed_at": iso_from_ms(item.get("sortTimeStamp")),
        "first_published_at": iso_from_ms(item.get("allowTimeStamp")),
    }


def _filter_values(raw_values):
    out = []
    for value in _list(raw_values):
        value = _dict(value)
        if isinstance(value.get("options"), list):          # sectioned: groups of options
            out += _filter_values(value["options"])
            continue
        vid = value.get("value") if value.get("value") is not None else value.get("id")
        name = _clean(value.get("name") or value.get("title"))
        if vid in (None, "") or not name:
            continue
        out.append({"id": str(vid), "name": name})
    return out


def parse_filter_vocabulary(filters_v2):
    """filtersV2 (the search form's own definitions) -> the attribute
    filters a caller can send back in `filters`: [{id, name, type, unit,
    values}]. Built-in inputs (price, sort, owner, photos, delivery, radius)
    have their own params and are left out."""
    out = []
    for section in _list(_dict(filters_v2).get("Sections")):
        for f in _list(_dict(section).get("Filters")):
            f = _dict(f)
            match = re.match(r"^params\[(\d+)\]$", str(f.get("id") or ""))
            ftype = f.get("type")
            if not match or ftype in ("hidden", "headerH2"):
                continue
            name = _clean(f.get("title") or f.get("defaultTitle"))
            if not name:
                continue
            kind = {"numericRange": "range", "slider": "range", "keywords": "text",
                    "boolean": "boolean"}.get(ftype, "select")
            values = _filter_values(f.get("values")) if kind in ("select", "range") else []
            out.append({
                "id": int(match.group(1)),
                "name": name,
                "type": kind,
                "unit": _clean(f.get("dimension") or f.get("currency")),
                "values": values,
            })
    return out


def _category_facets(rubricators):
    """rubricatorCategories (per-category result counts for this search) ->
    the top two levels: [{id, name, slug, count, filters, children}]."""
    roots = _list(_dict(rubricators).get("rubricatorCategories"))

    def node(n, depth):
        n = _dict(n)
        children = [node(c, depth + 1) for c in _list(n.get("subs") or n.get("children"))] if depth < 2 else []
        return {
            "id": _int(n.get("categoryId")),
            "name": _clean(n.get("name")),
            "slug": refs.category_slug(n.get("url")),
            "count": _int(n.get("count")),
            "filters": _filters_string(n.get("params")),
            "children": [c for c in children if c["name"]],
        }

    def keep(n):
        # editorial links (the "Журнал" magazine node) carry no listing slug
        n["children"] = [keep(c) for c in n["children"] if c["slug"]]
        return n

    out = []
    for root in roots:   # "Все категории"
        for child in _list(_dict(root).get("subs")):
            built = node(child, 1)
            if built["name"] and built["slug"]:
                out.append(keep(built))
    return out


# Default-valued params the site appends to every search URL it echoes.
_LINK_NOISE_RE = re.compile(r"(?<=[?&])(?:localPriority=0|radius=0|searchRadius=0|cd=1)(?:&|$)")


def _clean_search_link(path):
    if not path or not str(path).startswith("/"):
        return None
    link = refs.SITE + str(path)
    while True:
        cleaned = _LINK_NOISE_RE.sub("", link)
        if cleaned == link:
            break
        link = cleaned
    return link.rstrip("?&")


def parse_search(payload):
    """/web/1/js/items JSON -> {results, total_count, capped_count, ...}."""
    if not isinstance(payload, dict) or not isinstance(payload.get("catalog"), dict):
        raise ParseError("not a catalog answer")
    catalog = payload["catalog"]
    results = [parse_card(i) for i in _list(catalog.get("items")) if _dict(i).get("type") == "item"]
    core = _dict(payload.get("searchCore"))
    path = str(payload.get("url") or "")
    slug = path.split("?", 1)[0].strip("/").split("/", 1)[0] or None
    related = []
    for tag in _list(payload.get("seoTags")):
        tag = _dict(tag)
        if _clean(tag.get("title")):
            # a listing link: its ?q= IS the related search (ad_link would drop the query)
            related.append({"query": _clean(tag.get("title")), "link": _clean_search_link(tag.get("url"))})
    return {
        "results": results,
        "total_count": _int(payload.get("count")) or 0,
        "capped_count": _int(payload.get("totalCount")),
        "items_per_page": _int(payload.get("itemsOnPage")) or len(results),
        "page_type": _dict(payload.get("pageTypes")).get("page"),
        "link": _clean_search_link(path),
        "location": {
            "id": _int(core.get("locationId")),
            "name": _clean(core.get("locationName")),
            "slug": slug,
        },
        "category_id": _int(core.get("categoryId")),
        "categories": _category_facets(payload.get("rubricators")),
        "filters": parse_filter_vocabulary(payload.get("filtersV2")),
        "related_searches": related,
    }


# ---- ad page --------------------------------------------------------------------------------

def _param_rows(items):
    out = []
    for row in _list(items):
        row = _dict(row)
        name = _clean(row.get("title"))
        if not name:
            continue
        descriptions = row.get("descriptions")
        if isinstance(descriptions, list):
            value = ", ".join(v for v in (_clean(d) for d in descriptions) if v) or None
        else:
            value = _clean(row.get("description"))
        out.append({"id": _int(row.get("attributeId")), "name": name, "value": value})
    return out


def _group(block):
    data = _dict(_dict(block).get("data")) or _dict(block)
    rows = _param_rows(data.get("items"))
    return {"title": _clean(data.get("title")), "params": rows} if rows else None


def _item_seller(buyer, item):
    seller = _dict(buyer.get("seller"))
    item_seller = _dict(item.get("seller"))
    shop = _dict(item.get("shop"))
    fav = _dict(buyer.get("favoriteSeller"))
    public = _dict(buyer.get("publicProfile"))
    rating = _dict(buyer.get("rating"))
    contact = _dict(buyer.get("contactBarInfo"))
    verification = _dict(item_seller.get("verification"))
    link = fav.get("publicProfileLink") or public.get("link") or seller.get("shopUrl")
    slug = None
    if link:
        match = re.search(r"/brands/([^/?#]+)", str(link))
        slug = match.group(1) if match else None
    if not slug and shop.get("domain"):
        slug = shop.get("domain")
    key = fav.get("userKey")
    if not key:
        for badge in _list(_dict(item.get("sellerBadgeBar")).get("iconsBadges")):
            match = re.search(r"objectId=([0-9a-f]{32})&", str(_dict(_dict(badge).get("action")).get("deeplink") or ""))
            if match:
                key = match.group(1)
                break
    since = None
    for candidate in (seller.get("tenureSince"), seller.get("tenureMedal"),
                      item_seller.get("tenureMedal"), item_seller.get("tenureSince")):
        since = parse_month_year(candidate)
        if since:
            break
    badges = []
    bar = _dict(item.get("sellerBadgeBar"))
    badges += _badge_titles(bar.get("badges"))
    badges += [t for t in _badge_titles(bar.get("iconsBadges")) if t not in badges]
    avatar = _dict(seller.get("avatar"))
    labels = _dict(seller.get("labels"))
    reply = None
    for entry in _list(_dict(contact.get("contacts")).get("list")):
        if _dict(entry).get("type") == "messenger":
            reply = _clean(_dict(_dict(entry).get("value")).get("replyTime"))
    return {
        "id": key,
        "slug": slug,
        "name": _clean(seller.get("name")) or _clean(shop.get("name")),
        "link": refs.seller_link(slug) if slug else None,
        "type": _clean(labels.get("nominative")) or _clean(_dict(buyer.get("priceDataDTO")).get("sellerDesc")),
        "is_company": bool(seller.get("isCompany")) if "isCompany" in seller else None,
        "is_shop": bool(shop.get("isShop")) if "isShop" in shop else None,
        "manager": _clean(seller.get("manager")),
        "avatar": largest_image(avatar.get("images")) or avatar.get("defaultUrl") or (
            item_seller.get("avatar") if isinstance(item_seller.get("avatar"), str) else None),
        "on_avito_since": since,
        "reply_time": _clean(seller.get("replyTimeText")) or reply,
        "rating": {
            "score": _float(rating.get("scoreFloat")),
            "reviews_count": _int(rating.get("summary")),
            "active_reviews_count": _int(rating.get("activeReviewsCount")),
        } if rating else None,
        "active_ads_count": _int(public.get("summary") or fav.get("summary")),
        "closed_ads_count": _int(public.get("closedItemsCount")),
        "subscribers_count": _int(fav.get("subscriptionCounter")) or None,
        "is_verified": bool(verification.get("isVerifiedSeller")) if verification else None,
        "is_inn_verified": bool(verification.get("isInnVerified")) if verification else None,
        "badges": badges,
    }


def _market_price(buyer, item):
    imv = _dict(buyer.get("carMarketPrice")) or _dict(item.get("carMarketPrice"))
    if not imv:
        return None
    ranges = _dict(imv.get("priceRanges"))
    description = _dict(imv.get("priceDescription"))
    pros, cons = [], []
    for switch in _list(description.get("switches")):
        switch = _dict(switch)
        values = [v for v in (_clean(x) for x in _list(switch.get("list"))) if v]
        if switch.get("type") == "pros":
            pros += values
        elif switch.get("type") == "cons":
            cons += values
    accuracy = None
    for rng in _list(ranges.get("ranges")):
        accuracy = accuracy or _int(_dict(rng).get("accuracy"))
    badge = _dict(item.get("inlinePriceBadge"))
    return {
        "estimate": _int(imv.get("price")),
        "low": _int(ranges.get("priceLow")),
        "high": _int(ranges.get("priceHigh")),
        "accuracy": accuracy,
        "currency": refs.CURRENCY,
        "verdict": _clean(badge.get("title")),
        "pros": pros,
        "cons": cons,
    }


def _property_check(buyer):
    teaser = _dict(buyer.get("domotekaReportTeaser"))
    if not teaser:
        return None
    findings = []
    for row in _list(teaser.get("infoList")):
        row = _dict(row)
        text = _clean(row.get("text"))
        if text:
            findings.append({"text": text, "is_ok": row.get("icon") == "done"})
    if not findings:
        return None
    return {
        "title": _clean(teaser.get("title")),
        "cadastral_number": _clean(teaser.get("cadastralNumber")),
        "findings": findings,
    }


def _job_posting(buyer):
    job = _dict(buyer.get("googleJobPosting"))
    if not job:
        return None
    salary = _dict(job.get("baseSalary"))
    value = _dict(salary.get("value"))
    org = _dict(job.get("hiringOrganization"))
    place = _dict(_dict(job.get("jobLocation")).get("address"))
    employment = job.get("employmentType")
    return {
        "date_posted": _clean(job.get("datePosted")),
        "valid_through": _clean(job.get("validThrough")),
        "employment_type": ", ".join(employment) if isinstance(employment, list) else _clean(employment),
        "salary": {
            "min": _int(value.get("minValue")) or None,
            "max": _int(value.get("maxValue")) or None,
            "currency": _clean(salary.get("currency")),
            "period": _clean(value.get("unitText")),
        } if salary else None,
        "employer": _clean(org.get("name")),
        "address": _clean(place.get("streetAddress")) or _clean(place.get("addressLocality")),
    }


def parse_item(page, now=None):
    """An ad page -> the full ad record; None when the page carries no ad
    (the /not-found page, a different page type)."""
    data = hydration_data(page)
    if not data:
        if is_challenge_page(page):
            raise ParseError("firewall page, not an ad")
        return None
    loader = _dict(_dict(data.get("loaderData")).get("catalog-or-main-or-item"))
    buyer = _dict(loader.get("buyerItem"))
    item = _dict(buyer.get("item"))
    if not item.get("id"):
        return None
    category = _dict(item.get("category"))
    parent = _dict(category.get("parent"))
    location = _dict(item.get("location"))
    geo = _dict(item.get("geo"))
    coords = _dict(geo.get("coords")) or _dict(location.get("coords"))
    formatted = _dict(item.get("formattedPrice"))
    price_dto = _dict(buyer.get("priceDataDTO"))
    history = _dict(buyer.get("priceHistory"))
    view = _dict(buyer.get("viewStat"))
    contact = _dict(buyer.get("contactBarInfo"))
    contact_types = [_dict(c).get("type") for c in _list(_dict(contact.get("contacts")).get("list"))]
    images = [largest_image(i) for i in _list(item.get("imageUrls"))]
    images = [i for i in images if i]
    video = _dict(item.get("video")) or _dict(buyer.get("nativeVideo"))
    amount = _int(item.get("price")) if formatted.get("isHasValue", True) else None
    groups = {}
    for key, block in (("house", item.get("houseParams")), ("rent_terms", item.get("rentTermsParams")),
                       ("rules", item.get("rulesParams")), ("conditions", item.get("conditionParams")),
                       ("requirements", item.get("vacancyParams")),
                       ("tires", item.get("tiresConditionParams"))):
        group = _group(block)
        if group:
            groups[key] = group
    options = []
    for row in _list(_dict(buyer.get("advancedParamsBlock")).get("items")):
        row = _dict(row)
        name = _clean(row.get("title"))
        values = [v for v in (_clean(d) for d in _list(row.get("descriptions"))) if v]
        if name and values:
            options.append({"name": name, "values": values})
    price_list = []
    for row in _list(_dict(item.get("priceList")).get("items") if isinstance(item.get("priceList"), dict)
                     else item.get("priceList")):
        row = _dict(row)
        if _clean(row.get("title")):
            price_list.append({"service": _clean(row.get("title")), "price": _clean(row.get("price") or row.get("value"))})
    status_message = _clean(buyer.get("closedItemStatus"))
    breadcrumbs = []
    for crumb in _list(item.get("breadcrumbs")):
        crumb = _dict(crumb)
        if _clean(crumb.get("name")):
            breadcrumbs.append({"name": _clean(crumb.get("name")), "link": refs.ad_link(crumb.get("url"))})
    lat, lng = _float(coords.get("lat")), _float(coords.get("lng"))
    badges = _badge_titles(_dict(item.get("badgeBar")).get("badges"))
    return {
        "id": _int(item.get("id")),
        "title": _clean(item.get("title")),
        "link": refs.ad_link(item.get("url")) or refs.ad_link(item.get("id")),
        "status": {
            "is_active": bool(item.get("isActive")),
            "is_closed": bool(item.get("isClosed") or buyer.get("closedItem")),
            "is_reserved": bool(_dict(item.get("deliveryInfo")).get("isReserved")),
            "message": status_message,
        },
        "category": {
            "id": _int(category.get("id")) or _int(item.get("categoryId")),
            "slug": _clean(category.get("slug")),
            "micro_category_id": _int(item.get("microCategoryId")),
            "parent": {"id": _int(parent.get("id")) or _int(category.get("parentId")),
                       "slug": _clean(parent.get("slug")) or _clean(category.get("parentSlug"))}
            if (parent or category.get("parentId")) else None,
            "root_id": _int(item.get("rootCategoryId")),
            "root_slug": _clean(item.get("rootCategorySlug")),
        },
        "breadcrumbs": breadcrumbs,
        "price": {
            "amount": amount,
            "currency": refs.CURRENCY if amount is not None else None,
            "text": _clean(formatted.get("formatedString")) or _clean(buyer.get("priceString")),
            "unit": _clean(formatted.get("postfix")),
            "old_amount": _int(formatted.get("oldString")),
            "discount": _clean(formatted.get("discount")) if isinstance(formatted.get("discount"), str) else None,
            "was_lowered": bool(formatted.get("isWasLowered")),
            "deposit_and_commission": _clean(price_dto.get("depositCommission")),
        },
        "price_history": {
            "is_new": bool(history.get("isNew")),
            "has_increased": bool(history.get("hasIncreased")),
            "changes_count": _int(history.get("recordsCount")),
        } if history else None,
        "market_price": _market_price(buyer, item),
        "description": html_to_text(item.get("description") or item.get("descriptionHtml")),
        "params": _param_rows(_dict(buyer.get("paramsBlock")).get("items")),
        "param_groups": groups,
        "options": options,
        "price_list": price_list,
        "location": {
            "id": _int(item.get("locationId")),
            "name": _clean(location.get("name")),
            "slug": _clean(location.get("slug")),
            "address": _clean(geo.get("address")) or _clean(item.get("address")),
            "coordinates": {"latitude": lat, "longitude": lng} if lat is not None and lng is not None else None,
            "metro": _metro(geo.get("references")),
        },
        "images": images,
        "images_count": len(images),
        "video_link": (video.get("videoUrl") or video.get("url")) if isinstance(video.get("videoUrl") or video.get("url"), str) else None,
        "seller": _item_seller(buyer, item),
        "contact": {
            "has_phone": "phone" in contact_types,
            "has_messenger": "messenger" in contact_types,
            "phone": None,   # the site shows a masked / temporary number only
        },
        "stats": {
            "views_today": _int(view.get("todayViews")),
            "views_total": _int(view.get("totalViews")),
        },
        "badges": badges,
        "vehicle_type": _clean(item.get("vehicleType")),
        "property_check": _property_check(buyer),
        "job": _job_posting(buyer),
        "is_delivery_available": bool(_dict(item.get("deliveryInfo")).get("isDelivery")),
        # the day only: the page renders its time in the visitor's zone (parse_ru_day)
        "listed_date": parse_ru_day(item.get("sortFormatedDate"), now),
        "expires_at": iso_from_seconds(item.get("finishTime")),
    }


# ---- seller profile page -----------------------------------------------------------------

def _widgets(value):
    return {(_dict(w).get("name") or _dict(w).get("type")): _dict(_dict(w).get("value"))
            for w in _list(_dict(value).get("widgets"))}


def _profile_tabs(widget):
    """The profile's tabs: 'Главное' / 'Оплата' (about_v2 text blocks and
    photos) and 'Адрес' (geo: addresses with coordinates and metro) ->
    (text, images, addresses)."""
    texts, images, addresses = [], [], []
    for tab in _list(widget.get("tabs")):
        inner = _dict(_dict(tab).get("widget"))
        value = _dict(inner.get("value"))
        about = _dict(value.get("about_v2"))
        for block in _list(about.get("content")):
            block = _dict(block)
            if block.get("type") == "image":
                url = largest_image({k: v for k, v in _dict(block.get("value")).items() if _SIZE_KEY_RE.match(k)})
                if url:
                    images.append(url)
                continue
            raw = block.get("value")
            text = html_to_text(raw.get("text") if isinstance(raw, dict) else raw) if raw else None
            if text:
                texts.append(text)
        for place in _list(_dict(value.get("geo")).get("list")):
            place = _dict(place)
            coords = _dict(place.get("coordinates"))
            lat, lng = _float(coords.get("latitude")), _float(coords.get("longitude"))
            address = _clean(place.get("formattedAddress"))
            if address or lat is not None:
                addresses.append({
                    "address": address,
                    "coordinates": {"latitude": lat, "longitude": lng} if lat is not None and lng is not None else None,
                    "metro": _metro(place.get("geoReferences")),
                })
    return ("\n\n".join(texts) or None), images, addresses


def _review(entry):
    value = _dict(entry)
    answer = _dict(value.get("answer"))
    texts = []
    for section in _list(value.get("textSections")):
        section = _dict(section)
        text = _clean(section.get("text"))
        title = _clean(section.get("title"))
        if text:
            texts.append({"title": title, "text": html_to_text(section.get("text"))})
    images = [largest_image(i) for i in _list(value.get("images"))]
    return {
        "id": _int(value.get("id")),
        "author": _clean(value.get("title")),
        "author_avatar": largest_image(value.get("avatar")),
        "score": _int(value.get("score")),
        "date": parse_ru_date(value.get("rated")),
        "deal_stage": _clean(value.get("stageTitle")),
        "delivery": _clean(value.get("deliveryTitle")),
        "ad_title": _clean(value.get("itemTitle")),
        "text": "\n\n".join(t["text"] for t in texts if t["text"]) or None,
        "sections": texts if any(t["title"] for t in texts) else [],
        "images": [i for i in images if i],
        "answer": {
            "author": _clean(answer.get("title")),
            "text": html_to_text(answer.get("text")),
            "date": parse_ru_date(answer.get("answered")),
        } if answer.get("text") else None,
    }


def parse_reviews(payload):
    """/web/7/user/<key>/ratings (or the profile page's reviewsData) ->
    {reviews, next_offset, summary}. `summary` comes only with the page's
    first block (the score entry)."""
    payload = _dict(payload)
    reviews, summary = [], None
    for entry in _list(payload.get("entries")):
        entry = _dict(entry)
        if entry.get("type") == "rating":
            reviews.append(_review(entry.get("value")))
        elif entry.get("type") == "score":
            score = _dict(entry.get("value"))
            summary = {
                "score": _float(score.get("scoreFloat")),
                "reviews_count": _int(score.get("reviewCount")),
                "verdict": _clean(score.get("title")),
                "distribution": {str(_int(_dict(s).get("score"))): _int(_dict(s).get("count"))
                                 for s in _list(score.get("ratingStat")) if _int(_dict(s).get("score"))},
            }
    next_offset = None
    match = re.search(r"[?&]offset=(\d+)", str(payload.get("nextPage") or ""))
    if match:
        next_offset = int(match.group(1))
    return {"reviews": reviews, "next_offset": next_offset, "summary": summary}


def parse_seller_page(page):
    """A public profile page -> the seller record (profile, rating, ads
    counts, category counts, first review block). None for an unknown
    profile; ParseError when the page has no profile state at all."""
    state = mfe_state(page)
    if not state:
        raise ParseError("no profile state on the page")
    loader = _dict(state.get("loaderData"))
    init = _dict(loader.get("initData"))
    if init.get("status") == "not-found":
        return None
    value = _dict(_dict(init.get("result")).get("value"))
    data = _dict(value.get("data"))
    key = data.get("profileUserHash") or _dict(data.get("analyticParams")).get("userHash")
    if not key:
        return None
    widgets = _widgets(value)
    # premium shops get the "ml" layout: base_info_ml carries the rating,
    # "На Авито с …", badges and follower label; buttons stand in for contacts
    base = widgets.get("base_info") or widgets.get("base_info_ml") or {}
    rating = _dict(base.get("rating"))
    subscribe = _dict(base.get("subscribeInfo"))
    contacts = widgets.get("contacts") or {}
    items_widget = widgets.get("item_list_with_filters") or {}
    tabs = {t.get("type"): _int(t.get("count")) for t in _list(items_widget.get("tabs")) if isinstance(t, dict)}
    about, about_images, addresses = _profile_tabs(widgets.get("tabs_with_widgets") or {})
    selections = []
    for sel in _list(_dict(widgets.get("item_selections_public")).get("selections")):
        sel = _dict(sel)
        if _clean(sel.get("title")):
            url = str(sel.get("url") or "")
            url = re.sub(r"[?&]s=profile_selection", "", url)   # click-source tag
            selections.append({"title": _clean(sel.get("title")), "ads_count": _int(sel.get("subtitle")),
                               "link": refs.SITE + url if url.startswith("/") else None})
    search = _dict(loader.get("searchData"))
    catalog = _dict(search.get("profileCatalog"))

    def cat_node(node):
        node = _dict(node)
        return {"id": _int(node.get("id")), "name": _clean(node.get("name")), "count": _int(node.get("count")),
                "children": [cat_node(c) for c in _list(node.get("subs"))]}

    categories = cat_node(catalog.get("categories")).get("children") if catalog.get("categories") else []
    seo = _dict(loader.get("seoData")) or _dict(data.get("seo"))
    org = {}
    try:
        graph = fastjson.loads(seo.get("schemaOrg") or "{}").get("@graph") or []
        org = _dict(graph[0]) if graph else {}
    except (ValueError, TypeError, AttributeError):
        org = {}
    address = _dict(org.get("address"))
    contact_types = [_dict(c).get("type") for c in _list(contacts.get("list"))]
    contact_types += [{"messenger": "message", "call": "call", "phone": "call"}.get(k, k)
                      for b in _list(_dict(base.get("buttons")).get("list")) for k in _dict(b)]
    custom = data.get("customLink")
    slug = None
    if custom:
        match = re.search(r"/brands/([^/?#]+)", str(custom))
        slug = match.group(1) if match else None
    reviews = parse_reviews(loader.get("reviewsData"))
    return {
        "id": key,
        "slug": slug,
        "name": _clean(data.get("username")),
        "link": refs.seller_link(slug) if slug else f"{refs.SITE}/user/{key}/profile",
        "type": {"shop": "company", "person": "private"}.get(data.get("profileType"), data.get("profileType")),
        "avatar": _dict(data.get("avatar")).get("big") or _dict(data.get("avatar")).get("default"),
        "description": about,
        "images": about_images,
        "on_avito_since": parse_month_year(base.get("description")),
        "reply_time": _clean(_dict(contacts.get("replyTime")).get("text")),
        "contact": {"has_phone": "call" in contact_types, "has_messenger": "message" in contact_types,
                    "phone": None},
        "rating": {
            "score": _float(rating.get("scoreFloat")),
            "reviews_count": _int(rating.get("summary")) or _int(rating.get("useStarsReviewCount")),
            "active_reviews_count": _int(rating.get("activeReviewsCount")),
            "verdict": (reviews["summary"] or {}).get("verdict"),
            "distribution": (reviews["summary"] or {}).get("distribution"),
        } if rating else None,
        "subscribers_count": (_int(_dict(subscribe.get("subscribers")).get("title"))
                              or _int(_dict(base.get("subscribers")).get("label"))),
        "subscriptions_count": _int(_dict(subscribe.get("subscriptions")).get("title")),
        "active_ads_count": tabs.get("active") if tabs.get("active") is not None else _int(catalog.get("totalCount")),
        "closed_ads_count": tabs.get("closed"),
        "categories": categories,
        "selections": selections,
        "badges": (_badge_titles(_dict(widgets.get("badgebar_v3")).get("badges"))
                   or _badge_titles(_dict(base.get("badges")).get("values"))),
        "addresses": addresses or ([{"address": _clean(address.get("streetAddress")), "coordinates": None,
                                     "metro": []}] if _clean(address.get("streetAddress")) else []),
    }


def parse_seller_ads(payload):
    """/web/1/profile/items JSON -> {results, total_count, items_per_page}."""
    if not isinstance(payload, dict) or not isinstance(payload.get("catalog"), dict):
        raise ParseError("not a profile catalog answer")
    results = [parse_card(i) for i in _list(payload["catalog"].get("items")) if _dict(i).get("type") == "item"]
    return {
        "results": results,
        "total_count": _int(payload.get("foundCount")) if payload.get("foundCount") is not None
        else _int(payload.get("totalCount")),
    }


# ---- locations / category tree --------- --------------------------------------------------

def _names(names):
    names = _dict(names)
    return _clean(names.get("1")) or next((_clean(v) for v in names.values() if _clean(v)), None)


def parse_locations(payload):
    """/web/1/slocations JSON -> [{id, name, region}]."""
    rows = []
    for loc in _list(_dict(_dict(payload).get("result")).get("locations")):
        loc = _dict(loc)
        if loc.get("id") is None:
            continue
        parent = _dict(loc.get("parent"))
        rows.append({
            "id": _int(loc.get("id")),
            "name": _names(loc.get("names")),
            "region": {"id": _int(parent.get("id")), "name": _names(parent.get("names"))} if parent else None,
        })
    return rows


def parse_location_page(page):
    """A location's home page -> {id, name} from its search core (the exit
    IP never changes it: the slug in the path decides)."""
    match = re.search(r'"searchCore":\{"locationId":(\d+)', page or "")
    if not match:
        return None
    name = re.search(r'"searchCore":\{[^{}]*?"locationName":"([^"]*)"', page)
    return {"id": int(match.group(1)), "name": _clean(name.group(1)) if name else None}


def parse_category_tree(page):
    """Any page's header state -> the site's category navigation tree:
    [{id, name, slug, link, filters, children}]. `filters` is the
    attribute filter a node adds to its category (pass it with `category`
    to /avito/ads/search to search that node)."""
    state = preloaded_state(page)
    tree = _list(_dict(_dict(_dict(state).get("layout")).get("header")).get("categoryTree"))
    if not tree:
        raise ParseError("no category tree in the page header")

    def node(n):
        n = _dict(n)
        url = n.get("url")
        link = None
        if isinstance(url, str) and url.startswith("/") and "{" not in url:
            link = refs.SITE + url.split("?", 1)[0]
        return {
            "id": _int(n.get("categoryId")),
            "name": _clean(n.get("name")),
            "slug": refs.category_slug(url),
            "link": link,
            "filters": _filters_string(n.get("params")),
            "children": [node(c) for c in _list(n.get("subs")) if _clean(_dict(c).get("name"))],
        }

    roots = []
    for root in tree:
        roots += [node(c) for c in _list(_dict(root).get("subs"))]
    return roots


def total_pages(total, per_page, cap=None):
    """Pages a search can serve: the site serves at most `cap` results."""
    if not per_page:
        return 0
    reachable = min(total, cap) if cap else total
    return int(math.ceil(reachable / per_page)) if reachable else 0
