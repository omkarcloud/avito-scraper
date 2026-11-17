"""Offline tests for the Avito parsers, reference resolvers, the filter
syntax and the proof-of-work solver.

    python -m pytest avito/test_parsers.py -q

Fixtures are real www.avito.ru responses captured through a Russian
residential exit on 2026-10-24 (avito/fixtures/, gzipped; see the
avito/parsers.py docstring for the list). No network.
"""
import gzip
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from avito import api, fetch, parsers as P, refs  # noqa: E402

FX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)   # capture day


def fixture(name):
    with gzip.open(os.path.join(FX, name + ".gz"), "rt", encoding="utf-8") as f:
        text = f.read()
    return json.loads(text) if name.endswith(".json") else text


@pytest.fixture(scope="module")
def bmw():
    return P.parse_search(fixture("search_bmw_moscow.json"))


@pytest.fixture(scope="module")
def flat():
    return P.parse_item(fixture("item_flat_rent.html"), NOW)


@pytest.fixture(scope="module")
def car():
    return P.parse_item(fixture("item_car.html"), NOW)


@pytest.fixture(scope="module")
def shop():
    return P.parse_seller_page(fixture("seller_shop.html"))


# ---- refs --------------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("7726049894", 7726049894),
    ("https://www.avito.ru/moskva/avtomobili/bmw_x5_3.0_at_2025_16_000_km_7726049894", 7726049894),
    ("https://www.avito.ru/moskva/avtomobili/bmw_x5_7726049894?context=H4sIAAA&x=1", 7726049894),
    ("https://m.avito.ru/moskva/avtomobili/bmw_x5_7726049894", 7726049894),
    ("www.avito.ru/7726049894", 7726049894),
    ("https://avito.ru/sankt-peterburg/kvartiry/1-k._kvartira_47_m_310_et._8340003606/", 8340003606),
])
def test_resolve_ad(value, expected):
    assert refs.resolve_ad(value) == expected


@pytest.mark.parametrize("value", [
    "", "bmw", "12", "https://example.com/moskva/x_7726049894",
    "https://www.avito.ru/moskva/avtomobili", "https://www.avito.ru/brands/americanauto",
])
def test_resolve_ad_invalid(value):
    with pytest.raises(ValueError):
        refs.resolve_ad(value)


@pytest.mark.parametrize("value,expected", [
    ("americanauto", {"kind": "brand", "id": "americanauto"}),
    ("I13285073", {"kind": "brand", "id": "i13285073"}),
    ("https://www.avito.ru/brands/americanauto?src=sharing", {"kind": "brand", "id": "americanauto"}),
    ("https://www.avito.ru/brands/americanauto/items/all?f=x", {"kind": "brand", "id": "americanauto"}),
    ("https://www.avito.ru/user/6f089f8d4dbdc5987c77eb440d7612fe/profile",
     {"kind": "key", "id": "6f089f8d4dbdc5987c77eb440d7612fe"}),
    ("6F089F8D4DBDC5987C77EB440D7612FE", {"kind": "hash", "id": "6f089f8d4dbdc5987c77eb440d7612fe"}),
])
def test_resolve_seller(value, expected):
    assert refs.resolve_seller(value) == expected


@pytest.mark.parametrize("value", ["", "a", "Американ Авто", "https://www.avito.ru/moskva/avtomobili",
                                   "https://example.com/brands/x"])
def test_resolve_seller_invalid(value):
    with pytest.raises(ValueError):
        refs.resolve_seller(value)


@pytest.mark.parametrize("value,expected", [
    ("637640", {"id": 637640}),
    ("kazan", {"id": 650400}),                       # in the verified slug table
    ("tambov", {"slug": "tambov"}),                   # resolved from its page
    ("Казань", {"name": "Казань"}),
    ("Нижний Новгород", {"name": "Нижний Новгород"}),
    ("https://www.avito.ru/kazan/avtomobili?q=bmw", {"id": 650400}),
    ("https://www.avito.ru/tambov/avtomobili", {"slug": "tambov"}),
    ("https://www.avito.ru/all/avtomobili", {"id": refs.ALL_RUSSIA_ID}),
    ("all", {"id": refs.ALL_RUSSIA_ID}),
    ("Россия", {"id": refs.ALL_RUSSIA_ID}),
])
def test_resolve_location(value, expected):
    assert refs.resolve_location(value) == expected


@pytest.mark.parametrize("value", ["", "x", "https://www.avito.ru/brands/x", "https://example.com/kazan"])
def test_resolve_location_invalid(value):
    with pytest.raises(ValueError):
        refs.resolve_location(value)


@pytest.mark.parametrize("value,expected", [
    ("9", {"id": 9}),
    ("avtomobili", {"slug": "avtomobili"}),
    ("https://www.avito.ru/moskva/avtomobili/bmw-ASgBAgICAUTgtg3klyg?cd=1", {"slug": "avtomobili"}),
    ("https://www.avito.ru/sankt-peterburg/kvartiry/sdam-ASgBAgICAUSSA8gQ", {"slug": "kvartiry"}),
])
def test_resolve_category(value, expected):
    assert refs.resolve_category(value) == expected


@pytest.mark.parametrize("value", ["", "https://www.avito.ru/moskva", "Автомобили", "https://example.com/a/b"])
def test_resolve_category_invalid(value):
    with pytest.raises(ValueError):
        refs.resolve_category(value)


def test_parse_filters_forms():
    assert refs.parse_filters("110007:331253") == [("params[110007]", "331253")]
    assert refs.parse_filters("110008:331255|331257") == [
        ("params[110008][]", "331255"), ("params[110008][]", "331257")]
    assert refs.parse_filters("2687:-50000, 164669:2018-2022") == [
        ("params[2687][to]", "50000"), ("params[164669][from]", "2018"), ("params[164669][to]", "2022")]
    assert refs.parse_filters("110011:200-") == [("params[110011][from]", "200")]
    assert refs.parse_filters(None) == []


@pytest.mark.parametrize("value", ["110007", "abc:1", "110007:x", "2687:-", "110008:1|x"])
def test_parse_filters_invalid(value):
    with pytest.raises(ValueError):
        refs.parse_filters(value)


def test_ad_and_seller_links():
    assert refs.ad_link("/moskva/avtomobili/x_1?context=abc") == "https://www.avito.ru/moskva/avtomobili/x_1"
    assert refs.ad_link(7726049894) == "https://www.avito.ru/7726049894"
    assert refs.seller_link("americanauto") == "https://www.avito.ru/brands/americanauto"
    assert refs.seller_page_path({"kind": "key", "id": "ab" * 16}) == f"/user/{'ab' * 16}/profile"
    assert refs.category_slug("/all/avtomobili?cd=1") == "avtomobili"
    assert refs.category_slug("/all?cd=1") is None


# ---- proof-of-work ---------------------------------------------------------------------------

def test_solve_pow_matches_the_site_rule():
    nonce = fetch.solve_pow("4f1b0c2e-test", 3, max_complexity=5)
    digest = hashlib.sha256(f"4f1b0c2e-test:{nonce}".encode()).hexdigest()
    assert digest.startswith("000")
    # the first such nonce, like the page's own loop
    assert all(not hashlib.sha256(f"4f1b0c2e-test:{n}".encode()).hexdigest().startswith("000")
               for n in range(nonce))


def test_solve_pow_refuses_heavy_challenges():
    with pytest.raises(fetch.AvitoBlocked):
        fetch.solve_pow("x", 9, max_complexity=5)


def test_jwt_payload():
    import base64
    body = base64.urlsafe_b64encode(json.dumps({"id": "abc", "compl": 3}).encode()).decode().rstrip("=")
    assert fetch._jwt_payload(f"h.{body}.s") == {"id": "abc", "compl": 3}


def test_challenge_pages_are_recognised():
    assert P.is_challenge_page(fixture("challenge_pow.html"))
    assert P.is_challenge_page(fixture("challenge_ip_captcha.html"))
    assert not P.is_challenge_page(fixture("item_car.html"))
    with pytest.raises(P.ParseError):
        P.parse_item(fixture("challenge_ip_captcha.html"), NOW)


# ---- value helpers ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("сегодня в 11:42", "2026-10-24T08:42:00Z"),
    ("вчера в 23:41", "2026-10-23T20:41:00Z"),
    ("17 октября в 20:14", "2026-10-17T17:14:00Z"),
    ("15 сентября 2023", "2023-09-15"),
    ("2 июля 2024", "2024-07-02"),
    ("7 мая", "2026-05-07"),
    ("25 декабря", "2025-12-25"),     # a day-month after "now" is last year's
    ("7 дней назад", None),
    ("", None),
])
def test_parse_ru_date(text, expected):
    assert P.parse_ru_date(text, NOW) == expected


def test_parse_ru_day_keeps_the_day_only():
    assert P.parse_ru_day("сегодня в 11:42", NOW) == "2026-10-24"
    assert P.parse_ru_day("17 октября в 20:14", NOW) == "2026-10-17"
    assert P.parse_ru_day("15 сентября 2023", NOW) == "2023-09-15"
    assert P.parse_ru_day("", NOW) is None


def test_small_helpers():
    assert P.parse_month_year("октября 2012") == "2012-10"
    assert P.parse_month_year("На Авито с января 2023") == "2023-01"
    assert P.distance_meters(" 700 м") == 700
    assert P.distance_meters(" 1,8 км") == 1800
    assert P.largest_image({"208x208": "a", "636x636": "b", "472x472": "c"}) == "b"
    assert P.html_to_text("<p>a&nbsp;b</p><p>c<br>d</p>") == "a b\nc\nd"
    assert P.iso_from_ms(1792683696000) == "2026-10-22T15:41:36Z"
    assert P.total_pages(1731251, 50, cap=1500) == 30
    assert P.total_pages(79, 50) == 2
    assert P.total_pages(0, 50) == 0


# ---- search --------------------------------------------------------------------------------

def test_search_happy_path(bmw):
    assert bmw["total_count"] == 1731251
    assert bmw["capped_count"] == 1500
    assert bmw["items_per_page"] == 50
    assert len(bmw["results"]) == 50
    assert bmw["location"] == {"id": 637640, "name": "Москва", "slug": "moskva"}
    assert bmw["link"] == "https://www.avito.ru/moskva/transport?q=bmw"
    assert bmw["page_type"] == "catalog"


def test_search_card_fields(bmw):
    card = bmw["results"][0]
    assert card["id"] == 7839266605
    assert card["title"] == "BMW X7 3.0 AT, 2025, 496 км"
    assert card["link"] == "https://www.avito.ru/moskva/avtomobili/bmw_x7_3.0_at_2025_496_km_7839266605"
    assert card["price"]["amount"] == 13999000 and card["price"]["currency"] == "RUB"
    assert card["params_summary"].startswith("496 км, 3.0 AT")
    assert card["category"] == {"id": 9, "name": "Автомобили", "slug": "avtomobili", "root_id": 1}
    assert card["location"]["coordinates"] == {"latitude": 55.790529, "longitude": 37.569063}
    assert card["location"]["metro"][0] == {"name": "Динамо", "distance_meters": 700,
                                             "walking_time": "6–10 мин.", "line_colors": ["#4FB04F"]}
    assert card["seller"]["id"] == "americanauto"
    assert card["seller"]["link"] == "https://www.avito.ru/brands/americanauto"
    assert card["seller"]["rating"] == {"score": 5.0, "reviews_count": 57}
    assert card["seller"]["closed_ads_count"] == 724
    assert "1 владелец" in card["badges"]
    assert card["images_count"] == 23 and card["images"][0].startswith("https://")
    assert card["has_video"] is True and card["video_link"].startswith("https://kinescope.io/")
    assert card["listed_at"] == "2026-10-16T14:17:25Z"
    assert card["labels"] == ["Автомобили", "Автодилер"]
    for value in card.values():
        assert value != "" and value != "N/A"


def test_search_card_discounted_price(bmw):
    card = next(c for c in bmw["results"] if c["id"] == 8368666040)
    assert card["price"]["amount"] == 9800000          # the shown price
    assert card["price"]["discounted_amount"] == 9550000
    assert card["price"]["text"] == "9 800 000 ₽"


def test_search_card_sale_price(bmw):
    # saleIcon + discount step: the card shows the sale price, priceDetailed the pre-sale one
    card = next(c for c in bmw["results"] if c["id"] == 4541657835)
    assert card["price"]["amount"] == 4901
    assert card["price"]["text"] == "4 901 ₽"
    assert card["price"]["old_amount"] == 5270
    assert card["price"]["discount_percent"] == 7
    assert card["price"]["discounted_amount"] is None


def test_search_card_dealer_from_price(bmw):
    # "от 5 950 000 ₽" printed; value and stringWithoutDiscount are the undiscounted price
    card = next(c for c in bmw["results"] if c["id"] == 8410314402)
    assert card["price"]["amount"] == 6750000
    assert card["price"]["text"] == "от 5 950 000 ₽"
    assert card["price"]["discounted_amount"] == 5950000
    assert card["price"]["old_amount"] is None


def test_search_filters_and_facets(bmw):
    names = {f["name"] for f in bmw["filters"]}
    assert "Тип автомобиля" in names
    car_type = next(f for f in bmw["filters"] if f["id"] == 1283)
    assert {"id": "14756", "name": "С пробегом"} in car_type["values"]
    transport = bmw["categories"][0]
    assert transport["id"] == 1 and transport["slug"] == "transport" and transport["count"] == 1806280
    assert all(c["slug"] for c in transport["children"])     # the magazine link is dropped


def test_search_filtered_toyota():
    parsed = P.parse_search(fixture("search_toyota_filtered.json"))
    assert "user=1" in parsed["link"] and "q=toyota" in parsed["link"]
    assert any(f["type"] == "range" for f in parsed["filters"])
    assert any(c["price"]["monthly_payment"] for c in parsed["results"] if c["price"])


def test_search_vertical_landing_has_no_cards():
    parsed = P.parse_search(fixture("search_services_landing.json"))
    assert parsed["results"] == []
    assert parsed["page_type"] == "vertical"
    assert parsed["total_count"] == 452937


def test_search_no_match():
    parsed = P.parse_search(fixture("search_no_match.json"))
    assert parsed["results"] == [] and parsed["total_count"] == 0


def test_search_sparse_cards():
    flats = P.parse_search(fixture("search_flats_spb_page2.json"))
    first = flats["results"][0]
    assert first["price"]["unit"] == "в месяц"
    assert first["seller"] is None or first["seller"]["name"] is None or isinstance(first["seller"]["name"], str)
    # a facet's multi-value params ({"499": [5254]}) come back in the "|" form `filters` accepts
    facet_filters = [n["filters"] for n in api._walk(flats["categories"]) if n["filters"]]
    assert "201:1059,499:5254" in facet_filters
    for value in facet_filters:
        refs.parse_filters(value)
    jobs = P.parse_search(fixture("search_vacancies.json"))
    # a related search's link keeps its ?q= (it IS the search)
    tinkoff = next(r for r in jobs["related_searches"] if r["query"] == "тинькофф работа")
    assert tinkoff["link"].startswith("https://www.avito.ru/moskva/vakansii?q=")
    assert len(jobs["results"]) == 50
    job = jobs["results"][0]
    assert job["seller"]["name"] == "НОВАЯ ВАХТА -ПРОВЕРЕННЫЙ РАБОТОДАТЕЛЬ"
    assert job["price"]["text"].startswith("до 6 800")


def test_search_rejects_other_payloads():
    with pytest.raises(P.ParseError):
        P.parse_search({"result": []})


# ---- ad page ---------------------------------------------------------------------------------

def test_item_flat(flat):
    assert flat["id"] == 8340003606
    assert flat["link"] == "https://www.avito.ru/sankt-peterburg/kvartiry/1-k._kvartira_47_m_310_et._8340003606"
    assert flat["status"] == {"is_active": True, "is_closed": False, "is_reserved": False, "message": None}
    assert flat["price"]["amount"] == 40000 and flat["price"]["unit"] == "в месяц"
    assert flat["price"]["deposit_and_commission"] == "залог 40000 ₽, без комиссии"
    assert {"id": 550, "name": "Количество комнат", "value": "1"} in flat["params"]
    assert flat["param_groups"]["house"]["params"][0] == {"id": 503, "name": "Тип дома", "value": "кирпичный"}
    assert flat["param_groups"]["rent_terms"]["params"][0]["name"] == "Залог"
    assert flat["param_groups"]["rules"]["params"][1] == {"id": 110651, "name": "Можно с детьми", "value": "да"}
    assert flat["location"]["coordinates"] == {"latitude": 60.01711273, "longitude": 30.30851936}
    assert flat["location"]["metro"][1]["distance_meters"] == 1800
    assert flat["stats"] == {"views_today": 40, "views_total": 913}
    assert flat["property_check"]["findings"][0] == {"text": "1 собственник", "is_ok": True}
    assert flat["seller"]["id"] == "6f089f8d4dbdc5987c77eb440d7612fe"
    assert flat["seller"]["slug"] == "d4bb2d9aa50a2e062e3586c469c3fe6a"
    assert flat["seller"]["type"] == "Арендодатель" and flat["seller"]["is_company"] is False
    assert flat["seller"]["on_avito_since"] == "2010-01"
    assert flat["seller"]["active_ads_count"] == 2 and flat["seller"]["closed_ads_count"] == 38
    assert flat["contact"] == {"has_phone": True, "has_messenger": True, "phone": None}
    assert flat["listed_date"] == "2026-10-17" and "listed_at" not in flat
    assert flat["images_count"] == 16
    assert flat["description"].startswith("Cдaётcя уютная")


def test_item_car(car):
    assert car["market_price"]["estimate"] == 12011400
    assert car["market_price"]["low"] == 11530900 and car["market_price"]["high"] == 12503900
    assert car["market_price"]["verdict"] == "Выше рынка"
    assert car["options"][0] == {"name": "Управление климатом", "values": ["Климат-контроль многозонный"]}
    assert car["seller"]["slug"] == "i13285073" and car["seller"]["name"] == "АвтоКапитал"
    assert car["seller"]["rating"]["score"] == 4.4
    assert car["vehicle_type"] == "С пробегом"
    assert car["stats"]["views_total"] is None       # hidden by this dealer
    assert car["breadcrumbs"][2] == {"name": "Автомобили", "link": "https://www.avito.ru/all/avtomobili"}


def test_item_vacancy_job_block():
    job = P.parse_item(fixture("item_vacancy.html"), NOW)
    assert job["job"]["date_posted"] == "2026-07-03"
    assert job["job"]["salary"] == {"min": None, "max": 6800, "currency": "RUB", "period": "MONTH"}
    assert job["param_groups"]["requirements"]["params"][0]["name"] == "Опыт работы"
    assert job["images"] == []


def test_item_closed():
    closed = P.parse_item(fixture("item_closed.html"), NOW)
    assert closed["status"]["is_closed"] is True and closed["status"]["is_active"] is False
    assert closed["status"]["message"] == "Объявление снято с публикации."
    assert closed["listed_date"] == "2023-09-15"


def test_item_phone_category():
    phone = P.parse_item(fixture("item_phone.html"), NOW)
    assert phone["category"]["id"] == 84
    assert any(p["name"] == "Модель" and p["value"] == "iPhone 15" for p in phone["params"])


def test_item_not_found_page():
    assert P.parse_item(fixture("item_not_found.html"), NOW) is None


# ---- sellers ---------------------------------------------------------------------------------

def test_seller_shop(shop):
    assert shop["id"] == "fb6e01314a92d1147c6e24f90ed2e18d"
    assert shop["slug"] == "americanauto"
    assert shop["type"] == "company"
    assert shop["on_avito_since"] == "2023-01"
    assert shop["rating"]["score"] == 5.0 and shop["rating"]["reviews_count"] == 57
    assert shop["rating"]["distribution"] == {"5": 57, "4": 0, "3": 0, "2": 0, "1": 0}
    assert shop["subscribers_count"] == 3980
    assert shop["active_ads_count"] == 79 and shop["closed_ads_count"] == 722
    assert shop["categories"][0]["children"][0] == {"id": 9, "name": "Автомобили", "count": 78, "children": []}
    assert shop["addresses"][0]["address"] == "Москва, Ленинградский пр-т, 36с40"
    assert shop["addresses"][0]["metro"][0]["name"] == "Динамо"
    assert "Компания проверена" in shop["badges"]
    assert shop["selections"][0]["title"] == "BMW в наличии"
    assert "sellerId=fb6e01314a92d1147c6e24f90ed2e18d" in shop["selections"][0]["link"]
    assert len(shop["images"]) == 54


def test_seller_shop_ml_layout():
    # premium shops: base_info_ml / ml_tabs_with_widgets instead of base_info / contacts
    shop = P.parse_seller_page(fixture("seller_shop_ml.html"))
    assert shop["id"] == "4afa0831691496ecd552e062456cfaa2"
    assert shop["slug"] == "619925d45391185f2e387acad44fe3e9"
    assert shop["on_avito_since"] == "2026-05"
    assert shop["rating"]["score"] == 4.7 and shop["rating"]["reviews_count"] == 368
    assert shop["rating"]["active_reviews_count"] == 459
    assert shop["rating"]["distribution"]["1"] == 21
    assert shop["subscribers_count"] == 738
    assert "Данные подтверждены" in shop["badges"]
    assert shop["contact"]["has_messenger"] is True and shop["contact"]["has_phone"] is False
    assert shop["active_ads_count"] > 3000


def test_seller_person_and_private_profile():
    person = P.parse_seller_page(fixture("seller_person.html"))
    assert person["type"] == "private" and person["slug"] == "i122478657"
    assert person["contact"]["has_phone"] is False
    private = P.parse_seller_page(fixture("seller_private_profile.html"))
    assert private["id"] == "6f089f8d4dbdc5987c77eb440d7612fe"
    assert private["slug"] == "d4bb2d9aa50a2e062e3586c469c3fe6a"
    assert private["rating"]["distribution"]["1"] == 3


def test_seller_not_found():
    assert P.parse_seller_page(fixture("seller_not_found.html")) is None
    assert fetch._SELLER_NOT_FOUND_RE.search(fixture("seller_not_found.html"))
    assert not fetch._SELLER_NOT_FOUND_RE.search(fixture("seller_shop.html"))


class _Resp:
    def __init__(self, text, url="https://www.avito.ru/x"):
        self.text, self.url = text, url


def test_state_checks_separate_not_found_from_degraded_pages():
    # a rendered profile passes; the explicit not-found state is a 404
    fetch._check_seller(_Resp(fixture("seller_shop.html")), "seller x")
    with pytest.raises(fetch.AvitoNotFound):
        fetch._check_seller(_Resp(fixture("seller_not_found.html")), "seller x")
    # the same page served WITHOUT its profile state (site under load) is
    # retryable, never a 404
    degraded = fixture("seller_degraded.html")
    assert "data-mfe-state" in degraded
    with pytest.raises(fetch.AvitoUpstreamError) as err:
        fetch._check_seller(_Resp(degraded), "seller x")
    assert not isinstance(err.value, fetch.AvitoNotFound)
    # ads: only the /not-found redirect is a 404
    fetch._check_item(_Resp(fixture("item_car.html")), "ad 1")
    with pytest.raises(fetch.AvitoNotFound):
        fetch._check_item(_Resp(fixture("item_not_found.html"), "https://www.avito.ru/not-found?requrl=%2F1"), "ad 1")
    with pytest.raises(fetch.AvitoUpstreamError):
        fetch._check_item(_Resp(fixture("seller_shop.html"), "https://www.avito.ru/moskva/x_1"), "ad 1")
    # JSON routes must carry their state key
    fetch._expect('"catalog":', "the catalog")(_Resp('{"catalog": {}}'), "search")
    with pytest.raises(fetch.AvitoUpstreamError):
        fetch._expect('"catalog":', "the catalog")(_Resp('{"error": 1}'), "search")


def test_seller_page_without_state():
    with pytest.raises(P.ParseError):
        P.parse_seller_page("<html><body>nothing</body></html>")


def test_seller_ads_pages():
    first = P.parse_seller_ads(fixture("seller_ads_page1.json"))
    last = P.parse_seller_ads(fixture("seller_ads_page2.json"))
    # (page 1 was captured newest-first, page 2 in the default order)
    assert len(first["results"]) == 50 and first["total_count"] == 79
    assert len(last["results"]) == 29 and last["total_count"] == 79
    assert all(c["seller"] is None or c["seller"]["id"] in (None, "americanauto") for c in first["results"])


def test_reviews_page():
    page = P.parse_reviews(fixture("seller_reviews_page2.json"))
    assert len(page["reviews"]) == 25 and page["next_offset"] == 50
    review = page["reviews"][1]
    assert review["author"] == "Роман" and review["score"] == 5
    assert review["date"] == "2024-06-30" and review["deal_stage"] == "Сделка состоялась"
    assert review["ad_title"] == "Toyota Sequoia 3.5 AT, 2024, 13 км"
    answered = next(r for r in page["reviews"] if r["answer"])
    assert answered["answer"]["text"] and answered["answer"]["author"]


def test_reviews_negative_first():
    page = P.parse_reviews(fixture("seller_reviews_negative.json"))
    scores = [r["score"] for r in page["reviews"]]
    assert scores == sorted(scores)


# ---- locations / tree ------------------------------------------------------------

def test_locations():
    rows = P.parse_locations(fixture("locations_mos.json"))
    assert rows[0] == {"id": 637640, "name": "Москва", "region": None}
    assert {"id": 637642, "name": "Новая Москва", "region": {"id": 637640, "name": "Москва"}} in rows
    assert P.parse_locations(fixture("locations_kazan.json"))[0]["id"] == 650400
    assert P.parse_locations(fixture("locations_empty.json")) == []


def test_location_page():
    assert P.parse_location_page(fixture("location_page_kazan.html")) == {"id": 650400, "name": "Казань"}
    assert P.parse_location_page(fixture("item_not_found.html")) is None


def test_category_tree():
    tree = P.parse_category_tree(fixture("item_not_found.html"))
    assert len(tree) == 12
    transport = tree[0]
    assert transport["id"] == 1 and transport["slug"] == "transport"
    cars = transport["children"][0]
    assert cars == {"id": 9, "name": "Автомобили", "slug": "avtomobili",
                    "link": "https://www.avito.ru/all/avtomobili", "filters": None, "children": []}
    moto = transport["children"][1]
    assert moto["children"][0]["filters"] == "30:112"
    with pytest.raises(P.ParseError):
        P.parse_category_tree("<html></html>")


def test_category_slug_resolution_uses_plain_nodes(monkeypatch):
    tree = P.parse_category_tree(fixture("item_not_found.html"))
    monkeypatch.setattr(api, "_tree", lambda: tree)
    assert api.resolve_category({"slug": "avtomobili"}) == 9
    assert api.resolve_category({"slug": "kvartiry"}) == 24
    assert api.resolve_category({"id": 84}) == 84
    with pytest.raises(ValueError):
        api.resolve_category({"slug": "no_such_category"})


# ---- api: the profile-key fast path (fetch stubbed, no network) -----------------------------

KEY = "fb6e01314a92d1147c6e24f90ed2e18d"


def _no_page(*args, **kwargs):
    raise AssertionError("the profile page must not be fetched for a profile key")


def test_seller_ads_by_key_skips_the_profile_page(monkeypatch):
    monkeypatch.setattr(api, "cached_call", lambda ns, data, fn, ttl, **kw: fn(**data))
    monkeypatch.setattr(api.fetch, "fetch_seller_page", _no_page)
    monkeypatch.setattr(api.fetch, "fetch_seller_ads", lambda *a, **k: fixture("seller_ads_page1.json"))
    out = api._seller_ads({"kind": "key", "id": KEY}, None, "newest", 1)
    assert out["seller"] == {"id": KEY, "slug": "americanauto",
                             "name": "Американ Авто - компания по продаже авто из США и Европы",
                             "link": "https://www.avito.ru/brands/americanauto"}
    assert len(out["results"]) == 50
    assert out["pagination"] == {"page": 1, "items_per_page": 50, "total_pages": 2, "total_count": 79}


def test_seller_ads_unknown_key_is_404_and_hash_falls_back(monkeypatch):
    monkeypatch.setattr(api, "cached_call", lambda ns, data, fn, ttl, **kw: fn(**data))
    calls = []

    def ads(key, *a, **k):
        calls.append(key)
        return {"status": "incorrect-data", "result": {"messages": {}}} if key == "0" * 32 \
            else fixture("seller_ads_page2.json")

    monkeypatch.setattr(api.fetch, "fetch_seller_ads", ads)
    monkeypatch.setattr(api.fetch, "fetch_seller_page", _no_page)
    with pytest.raises(fetch.AvitoNotFound):
        api._seller_ads({"kind": "key", "id": "0" * 32}, None, "relevance", 1)
    # a bare 32-hex that is no key: treated as a private seller's brand slug
    monkeypatch.setattr(api.fetch, "fetch_seller_page", lambda ref: fixture("seller_private_profile.html"))
    out = api._seller_ads({"kind": "hash", "id": "0" * 32}, None, "relevance", 2)
    assert out["seller"]["id"] == "6f089f8d4dbdc5987c77eb440d7612fe"
    assert calls == ["0" * 32, "0" * 32, "6f089f8d4dbdc5987c77eb440d7612fe"]
    # ... and it goes straight to the brand page, not /user/<hex>/profile first
    pages = []
    monkeypatch.setattr(api.fetch, "fetch_seller_page", lambda ref: pages.append(ref) or fixture("seller_private_profile.html"))
    api._seller_ads({"kind": "hash", "id": "0" * 32}, None, "relevance", 1)
    assert pages == [{"kind": "brand", "id": "0" * 32}]


def test_seller_reviews_by_key(monkeypatch):
    monkeypatch.setattr(api, "cached_call", lambda ns, data, fn, ttl, **kw: fn(**data))
    monkeypatch.setattr(api.fetch, "fetch_seller_page", _no_page)
    monkeypatch.setattr(api.fetch, "fetch_seller_reviews", lambda *a, **k: fixture("seller_reviews_page2.json"))
    out = api._seller_reviews({"kind": "key", "id": KEY}, "newest", None, 2)
    assert len(out["results"]) == 25 and out["pagination"]["has_next"] is True
    assert out["seller"]["id"] == KEY
    # a full page the ratings API sent without nextPage still pages on
    page2 = fixture("seller_reviews_page2.json")
    page2.pop("nextPage", None)
    monkeypatch.setattr(api.fetch, "fetch_seller_reviews", lambda *a, **k: page2)
    assert api._seller_reviews({"kind": "key", "id": KEY}, "newest", None, 2)["pagination"]["has_next"] is True
    # an empty first page from an unknown key is a 404, not an empty list
    monkeypatch.setattr(api.fetch, "fetch_seller_reviews", lambda *a, **k: {"entries": []})
    monkeypatch.setattr(api.fetch, "fetch_seller_ads", lambda *a, **k: {"status": "incorrect-data"})
    with pytest.raises(fetch.AvitoNotFound):
        api._seller_reviews({"kind": "key", "id": "0" * 32}, "newest", None, 1)
