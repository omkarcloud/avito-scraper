"""The /avito/* endpoint functions (route_glue's route_table calls them with
the schema's loaded kwargs). Every function caches through
cache.cached_call keyed on its validated params.

Shared upstream reads:
  * the seller profile page is parsed ONCE per seller (avito.seller):
    sellers/details returns it, sellers/ads and sellers/reviews take the
    profile key from it, so a client walking all three pays one page fetch
    plus the list call it asked for. Given the 32-hex profile key itself,
    ads and reviews skip the page (the flakiest surface) and call their
    JSON route directly;
  * the category tree (avito.category_tree) serves categories/list and
    resolves category slugs for search and seller ads;
  * location names and slugs resolve once (avito.location_name for 7 days,
    avito.location_slug for 30) and are reused by every search.
"""
from datetime import datetime, timezone

from cache import cached_call
from avito import cache_config as ttl
from avito import fetch, parsers, refs

SEARCH_PAGE_SIZE = 50          # the site's catalog page
SEARCH_RESULT_CAP = 1500       # js/items serves at most 30 pages of 50
SELLER_ADS_PAGE_SIZE = 50      # /web/1/profile/items accepts limit=50
REVIEWS_PAGE_SIZE = 25         # the profile page's own review page size
LANDING_PAGE_TYPES = ("vertical", "main")


# ---- reference resolution ------------------------------------------------------------------

def _location_by_slug(slug):
    try:
        record = parsers.parse_location_page(fetch.fetch_location_page(slug))
    except fetch.AvitoNotFound:
        record = None
    if not record:
        raise ValueError(f"location: unknown Avito location '{slug}' (check the slug in an avito.ru link, "
                         "or pass a location id or name; see /avito/locations/search).")
    return {**record, "slug": slug}


def _location_by_name(name):
    rows = parsers.parse_locations(fetch.fetch_locations(name, 10))
    if not rows:
        raise ValueError(f"location: no Avito location matches '{name}' (see /avito/locations/search).")
    wanted = name.strip().lower()
    exact = [r for r in rows if (r["name"] or "").lower() == wanted]
    best = (exact or rows)[0]
    return {"id": best["id"], "name": best["name"]}


def resolve_location(location):
    """A loaded `location` value ({id} | {slug} | {name} | None) -> {id, ...}."""
    if not location:
        return {"id": refs.ALL_RUSSIA_ID}
    if location.get("id"):
        return {"id": location["id"]}
    if location.get("slug"):
        return cached_call("avito.location_slug", {"slug": location["slug"]},
                           lambda **_: _location_by_slug(location["slug"]), ttl.LOCATION_SLUG_CACHE)
    return cached_call("avito.location_name", {"name": location["name"].lower()},
                       lambda **_: _location_by_name(location["name"]), ttl.LOCATIONS_CACHE)


def _tree():
    return cached_call("avito.category_tree", {},
                       lambda **_: parsers.parse_category_tree(fetch.fetch_category_tree_page()),
                       ttl.CATEGORY_TREE_CACHE)


def _walk(nodes):
    for node in nodes or []:
        yield node
        yield from _walk(node.get("children"))


def resolve_category(category):
    """A loaded `category` value ({id} | {slug} | None) -> the numeric id."""
    if not category:
        return None
    if category.get("id"):
        return category["id"]
    slug = category["slug"]
    nodes = [n for n in _walk(_tree()) if n.get("slug") == slug and n.get("id")]
    # a plain category node (no sub-category filters) owns its slug; the
    # menu shows some categories only as filtered entries ("Купить жильё" =
    # kvartiry + 201:1059), which still carry the category's own id
    plain = [n for n in nodes if not n.get("filters")]
    if plain or nodes:
        return (plain or nodes)[0]["id"]
    raise ValueError(f"category: unknown Avito category slug '{slug}' (see /avito/categories/list).")


def _category_info(category_id, facets):
    """{id, name, slug} for the searched category, from the search's own
    facets (no extra fetch)."""
    if not category_id:
        return None
    for node in _walk(facets):
        if node.get("id") == category_id and not node.get("filters"):
            return {"id": category_id, "name": node.get("name"), "slug": node.get("slug")}
    return {"id": category_id, "name": None, "slug": None}


# ---- ads/search ------------------------------------------------------------------------------

def _search_query(query, location_id, category_id, price_min, price_max, filters, seller_type,
                  with_photos_only, delivery_only, title_only, radius):
    q = [("locationId", location_id)]
    if category_id:
        q.append(("categoryId", category_id))
    if query:
        q.append(("name", query))
    if price_min is not None:
        q.append(("pmin", price_min))
    if price_max is not None:
        q.append(("pmax", price_max))
    if seller_type:
        q.append(("user", refs.SELLER_TYPES[seller_type]))
    if with_photos_only:
        q.append(("i", 1))
    if delivery_only:
        q.append(("d", 1))
    if title_only:
        q.append(("bt", 1))
    if radius:
        q.append(("radius", radius))
    q += [tuple(p) for p in (filters or [])]
    return q


def _search(query, location, category, price_min, price_max, filters, seller_type,
            with_photos_only, delivery_only, title_only, radius, sort, page):
    where = resolve_location(location)
    category_id = resolve_category(category)
    base = _search_query(query, where["id"], category_id, price_min, price_max, filters, seller_type,
                         with_photos_only, delivery_only, title_only, radius)
    applied_sort = sort
    payload = fetch.fetch_search(base + ([("s", refs.SORTS[sort])] if sort != "relevance" else []), page)
    parsed = parsers.parse_search(payload) if payload is not None else None
    if (parsed and not parsed["results"] and parsed["page_type"] in LANDING_PAGE_TYPES
            and sort == "relevance"):
        # A root category without a query renders the site's category
        # landing (no ads) under the default sort; any explicit sort lists
        # the ads (verified 2026-10-24), so the newest-first list is served.
        applied_sort = "newest"
        payload = fetch.fetch_search(base + [("s", refs.SORTS["newest"])], page)
        parsed = parsers.parse_search(payload) if payload is not None else None
    if parsed is None:   # a page past the site's last one
        return {
            "query": query,
            "location": {"id": where["id"], "name": where.get("name"), "slug": where.get("slug")},
            "category": {"id": category_id, "name": None, "slug": None} if category_id else None,
            "sort": applied_sort, "link": None, "results": [], "categories": [], "filters": [],
            "related_searches": [],
            "pagination": {"page": page, "items_per_page": SEARCH_PAGE_SIZE, "has_next": False,
                           "total_pages": None, "total_count": None},
        }
    total = parsed["total_count"]
    return {
        "query": query,
        "location": parsed["location"],
        "category": _category_info(category_id, parsed["categories"]),
        "sort": applied_sort,
        "link": parsed["link"],
        "results": parsed["results"],
        "categories": parsed["categories"],
        "filters": parsed["filters"],
        "related_searches": parsed["related_searches"],
        "pagination": {
            "page": page,
            "items_per_page": SEARCH_PAGE_SIZE,
            # page 31 answers HTTP 400 on every search; the landing fallback
            # (newest-first) reports its full count as `totalCount`
            "total_pages": parsers.total_pages(total, SEARCH_PAGE_SIZE,
                                               cap=min(parsed["capped_count"] or SEARCH_RESULT_CAP,
                                                       SEARCH_RESULT_CAP)),
            "total_count": total,
        },
    }


def ads_search(**data):
    return cached_call("avito.search", data, _search, ttl.SEARCH_CACHE)


# ---- ads/details -----------------------------------------------------------------------------

def _ad(ad):
    page, _ = fetch.fetch_item(ad)
    record = parsers.parse_item(page, now=datetime.now(timezone.utc))
    if record is None:   # fetch_item already mapped /not-found; this is a layout change
        raise parsers.ParseError(f"ad {ad}: the page carries no ad record")
    return record


def ad_details(**data):
    return cached_call("avito.ad", data, _ad, ttl.AD_CACHE)


# ---- locations/search / categories/list ----------------------------------------------------

def _locations(query, limit):
    rows = parsers.parse_locations(fetch.fetch_locations(query, limit))
    return {"query": query, "count": len(rows), "results": rows[:limit]}


def locations_search(**data):
    return cached_call("avito.locations", data, _locations, ttl.LOCATIONS_CACHE)


def categories_list(**data):
    tree = _tree()
    return {"count": len(tree), "results": tree}


# ---- sellers ---------------------------------------------------------------------------------

def _seller_record(kind, id):
    if kind == "hash":
        # a bare 32-hex value: a profile key, else a private seller's brand slug
        try:
            page = fetch.fetch_seller_page({"kind": "key", "id": id})
        except fetch.AvitoNotFound:
            page = fetch.fetch_seller_page({"kind": "brand", "id": id})
    else:
        page = fetch.fetch_seller_page({"kind": kind, "id": id})
    record = parsers.parse_seller_page(page)
    if record is None:
        raise fetch.AvitoNotFound(f"seller {id} not found")
    return record


def _seller(seller):
    return cached_call("avito.seller", {"kind": seller["kind"], "id": seller["id"]},
                       _seller_record, ttl.SELLER_CACHE)


def _identity(record):
    return {"id": record["id"], "slug": record["slug"], "name": record["name"], "link": record["link"]}


def seller_details(seller):
    """The public profile: name, type, rating and its distribution, ad
    counts per category, badges, subscribers, addresses, photos."""
    return _seller(seller)


def _key_identity(key, cards=(), reviews=()):
    """Seller identity without the profile page: from a catalog card's
    seller block, else from a review answer's profile link."""
    for card in cards:
        seller = card.get("seller") or {}
        if seller.get("id") or seller.get("name"):
            return {"id": key, "slug": seller.get("id"), "name": seller.get("name"), "link": seller.get("link")}
    for review in reviews:
        answer = review.get("answer") or {}
        if answer.get("author"):
            return {"id": key, "slug": None, "name": answer["author"], "link": f"{refs.SITE}/user/{key}/profile"}
    return {"id": key, "slug": None, "name": None, "link": f"{refs.SITE}/user/{key}/profile"}


def _unknown_key(key):
    """The profile catalog answers {"status": "incorrect-data"} for a key
    that names no profile (2026-10-24); the ratings API answers an empty
    list, indistinguishable from a seller without reviews."""
    probe = fetch.fetch_seller_ads(key, 1, 1)
    return isinstance(probe, dict) and probe.get("status") == "incorrect-data"


def _ads_pagination(page, total):
    if total is None:
        return {"page": page, "items_per_page": SELLER_ADS_PAGE_SIZE, "has_next": False,
                "total_pages": None, "total_count": None}
    return {"page": page, "items_per_page": SELLER_ADS_PAGE_SIZE,
            "total_pages": parsers.total_pages(total, SELLER_ADS_PAGE_SIZE), "total_count": total}


def _seller_ads(seller, category, sort, page):
    category_id = resolve_category(category)
    # A profile key goes straight to the catalog JSON: the profile page is
    # the flakiest surface (rendered without its state on ~half the loads,
    # 2026-10-24) and is only needed to turn a slug into a key.
    if seller["kind"] in ("key", "hash"):
        payload = fetch.fetch_seller_ads(seller["id"], page, SELLER_ADS_PAGE_SIZE, refs.SORTS[sort], category_id)
        if not (isinstance(payload, dict) and payload.get("status") == "incorrect-data"):
            parsed = parsers.parse_seller_ads(payload) if payload is not None else {"results": [], "total_count": None}
            return {"seller": _key_identity(seller["id"], parsed["results"]), "category_id": category_id,
                    "sort": sort, "results": parsed["results"], "pagination": _ads_pagination(page, parsed["total_count"])}
        if seller["kind"] == "key":
            raise fetch.AvitoNotFound(f"seller {seller['id']} not found")
        # a bare 32-hex value that is not a key: a private seller's brand slug
        seller = {"kind": "brand", "id": seller["id"]}
    record = _seller(seller)
    payload = fetch.fetch_seller_ads(record["id"], page, SELLER_ADS_PAGE_SIZE, refs.SORTS[sort], category_id)
    parsed = parsers.parse_seller_ads(payload) if payload is not None else {"results": [], "total_count": None}
    return {
        "seller": _identity(record),
        "category_id": category_id,
        "sort": sort,
        "results": parsed["results"],
        "pagination": _ads_pagination(page, parsed["total_count"]),
    }


def seller_ads(**data):
    return cached_call("avito.seller_ads", data, _seller_ads, ttl.SELLER_ADS_CACHE)


def _reviews_page(key, sort, with_photos_only, page):
    payload = fetch.fetch_seller_reviews(key, (page - 1) * REVIEWS_PAGE_SIZE, REVIEWS_PAGE_SIZE,
                                         refs.REVIEW_SORTS[sort], bool(with_photos_only))
    return parsers.parse_reviews(payload)


def _reviews_has_next(parsed):
    """A full page without nextPage still has a next page: the worst case
    is one empty page after an exactly-full last one."""
    reviews = parsed["reviews"]
    return bool(reviews) and (parsed["next_offset"] is not None or len(reviews) >= REVIEWS_PAGE_SIZE)


def _seller_reviews(seller, sort, with_photos_only, page):
    # The ratings API publishes no total for the list (the profile's review
    # count covers starred reviews only): `has_next` follows its nextPage,
    # which it now and then leaves off a full page (_reviews_has_next).
    if seller["kind"] in ("key", "hash"):
        parsed = _reviews_page(seller["id"], sort, with_photos_only, page)
        known = bool(parsed["reviews"] or parsed["summary"]) or not _unknown_key(seller["id"])
        if known:
            summary = parsed["summary"]
            return {
                "seller": _key_identity(seller["id"], reviews=parsed["reviews"]),
                "rating": {**summary, "active_reviews_count": None} if summary else None,
                "sort": sort,
                "results": parsed["reviews"],
                "pagination": {"page": page, "items_per_page": REVIEWS_PAGE_SIZE,
                               "has_next": _reviews_has_next(parsed),
                               "total_pages": None, "total_count": None},
            }
        if seller["kind"] == "key":
            raise fetch.AvitoNotFound(f"seller {seller['id']} not found")
        # not a key (the catalog said so): a private seller's brand slug
        seller = {"kind": "brand", "id": seller["id"]}
    record = _seller(seller)
    parsed = _reviews_page(record["id"], sort, with_photos_only, page)
    return {
        "seller": _identity(record),
        "rating": record.get("rating"),
        "sort": sort,
        "results": parsed["reviews"],
        "pagination": {"page": page, "items_per_page": REVIEWS_PAGE_SIZE,
                       "has_next": _reviews_has_next(parsed),
                       "total_pages": None, "total_count": None},
    }


def seller_reviews(**data):
    return cached_call("avito.seller_reviews", data, _seller_reviews, ttl.SELLER_REVIEWS_CACHE)
