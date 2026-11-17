"""Avito (www.avito.ru) transport: plain curl_cffi with browser-impersonated
TLS, no browser. Egress: one sticky exit per session (config.avito_proxy(),
knob AVITO_EXIT — a Russian residential exit, see the probe table in
config.py). Sessions live in a process-wide identity_pool.Pool in exclusive
mode (since 2026-11-09): a request borrows one for ONE call and hands it
back, so the next request on any of the pod's 64 threads reuses its
proof-of-work clearance instead of solving its own (before, each thread
kept a session and solved the PoW per thread). A retryable failure marks the
session dead (retired on give-back), so the retry rides a fresh exit and
solves a fresh challenge; a session is also retired after AVITO_SESSION_USES
calls or AVITO_SESSION_MAX_AGE seconds (the clearance's unblock_ttl).

The site's firewall (research + build 2026-10-23/24):

  * Proof-of-work (HTTP 439, "Доступ ограничен: проверка безопасности").
    Every new session gets it on its first request, page or JSON route
    alike, from every exit tried (the pod's own IP, Russian and Moroccan
    residential, a home ISP in India). It is not a browser check: the
    response sets a `pow_challenge` cookie; POST /web/3/firewallPow/get
    {challenge} returns a JWT whose payload carries {id, compl}; the
    answer is the first nonce whose sha256("<id>:<nonce>") hex starts with
    `compl` zeros (compl was 3: ~4k hashes, ~10 ms); POST
    /web/3/firewallPow/verify {challenge: <jwt>, nonce} -> {"verified":
    true, "unblock_ttl": 420-600}. The session's cookie jar then passes
    until the ttl runs out, and the next 439 is solved the same way.
    The page's own script does exactly this (pow page fixture).

  * IP captcha (HTTP 429, "Доступ ограничен: проблема с IP"). A GeeTest v4
    page (captcha id 2d9c743cf7d63dbc9db578a608196bcd, hCaptcha / an image
    captcha as alternatives) served per REQUEST, not per exit: about 1 call
    in 8 on a residential RU exit, more on the pod's own Google Cloud IP,
    and the next call on the same exit usually passes. CapSolver solves
    the GeeTest in ~4 s but the site's /web/3/firewallCaptcha/verify
    answered `verified: false` to every token (proxyless and same-exit,
    2026-10-24), so a 429 is AvitoBlocked: the session is dropped and the
    retry rides a fresh exit. A JSON route answers the same as a bare 429.

  * The search box's suggest route (POST /web/3/suggest) answered 429 to
    23 of 24 calls on fresh AND warmed sessions (2026-10-24), so it is not
    served.

Upstream surfaces (all GET unless noted; JSON ones need
x-requested-with: XMLHttpRequest, without it they answer 403):

  SEARCH     /web/1/js/items?locationId&categoryId&name&pmin&pmax&s&user
             &i&d&bt&radius&params[..]&page — the site's own catalog JSON,
             50 cards per page, at most 30 pages (`totalCount` caps at
             1500; page 31 answers HTTP 400). Without locationId the site
             picks the exit IP's city, so a location is always sent.
  ITEM       /<id> -> 301 to the canonical page; the server-rendered page
             carries window.__staticRouterHydrationData. Unknown ids
             redirect to /not-found (HTTP 200).
  SELLER     /brands/<slug> or /user/<profile key>/profile — server-rendered
             public profile, state in <script type="mime/invalid"
             data-mfe-state>; unknown = initData.status "not-found".
  SELLER ADS /web/1/profile/items?sellerId=<key>&shortcut=active&page&limit
  REVIEWS    /web/7/user/<key>/ratings?limit&offset&sortRating&photoOnly
  LOCATIONS  /web/1/slocations?q&limit[&locationId]
  TREE       /not-found — the smallest page (~300 KB) whose header carries
             the site's full category navigation tree.
  LOC PAGE   /<location slug> — read once per slug for its numeric id.

Failure taxonomy (scraper_errors, mapped to HTTP by route_glue):
  AvitoUpstreamError  transport failure / 5xx / unparseable answer — retryable
  AvitoBlocked        439 not cleared / 429 captcha / 403          — retryable
  AvitoBadRequest     upstream 400                                 — never retried
  AvitoNotFound       404 / /not-found redirect / unknown profile  — never retried
"""
import base64
import hashlib
import fastjson
import os
import re
import sys
import time
from urllib.parse import urlencode, urlparse

# Allow direct execution (python avito/fetch.py): flat imports resolve like
# under the server. Idempotent when imported normally.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import egress
import identity_pool
from scraper_errors import BadRequest, Blocked, NotFound, UpstreamError
from avito import refs
from scraper_utils import retry

HOST = refs.SITE
IMPERSONATE = "chrome"
PAGE_TIMEOUT = 40      # item / profile pages are 0.6-1.7 MB
JSON_TIMEOUT = 30      # js/items answers 1-2 MB

PAGE_HEADERS = {
    "accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "upgrade-insecure-requests": "1",
}
JSON_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "x-requested-with": "XMLHttpRequest",
}

POW_TITLE = "проверка безопасности"     # "Доступ ограничен: проверка безопасности"
IP_CAPTCHA_TITLE = "проблема с IP"       # "Доступ ограничен: проблема с IP"
_TITLE_RE = re.compile(r"<title>([^<]*)</title>", re.I)
# an unknown profile: the page state's loader answers
# {"initData":{"result":{"message":"Пользователь не найден"},"status":"not-found"}
_SELLER_NOT_FOUND_RE = re.compile(r'"initData":\{"result":\{"message":"[^"]*"\},"status":"not-found"')
# a rendered profile: its loader carries {"initData":{"result":{"type":"extended","value":...}}}.
# Under load the site serves the SAME page with no initData at all, for real
# and unknown sellers alike (seen 2026-10-24 from the pod and the Mac) — that
# is a site-side degradation, retried on a fresh exit, never a 404.
_SELLER_OK_RE = re.compile(r'"loaderData":\{"initData":\{"result":\{"type":')


class AvitoUpstreamError(UpstreamError):
    """Transport failure, 5xx or an unparseable answer — retryable."""


class AvitoBlocked(AvitoUpstreamError, Blocked):
    """Firewall: proof-of-work not cleared, IP captcha (429) or 403 — retryable on a fresh exit."""


class AvitoBadRequest(BadRequest):
    """Upstream 400 — never retried."""


class AvitoNotFound(NotFound):
    """Ad / seller / location does not exist — never retried."""


# ---- pooled sessions ---------------------------------------------------------------------
# One pool, one key: page and JSON calls share a session (the proof-of-work
# clearance in its jar covers both). A session owns ONE curl handle, so the
# pool lends it to one request at a time; threads take turns on it.

class _Session(identity_pool.Identity):
    """One curl session on one sticky exit; the PoW clearance lives in its
    jar. The pool (identity_pool.Identity) keeps budget / age / refusal."""

    def __init__(self, proxy):
        from curl_cffi import requests as curl_requests
        super().__init__(exit=proxy)
        self.curl = curl_requests.Session(impersonate=IMPERSONATE, use_thread_local_curl=False)
        if proxy:
            self.curl.proxies = {"http": proxy, "https": proxy}
        # owner rule 2026-10-24: the language matches the exit's country
        self.curl.headers.update({"accept-language": egress.accept_language(proxy)})
        self.closed = False

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.curl.close()
        except Exception:
            pass


def _mint(key, hint=None):
    """The pool's mint: a cold session on a fresh exit (the first call meets
    the 439 and clears it)."""
    return _Session(config.avito_proxy())


_sessions = identity_pool.Pool(
    "avito", _mint, mode="exclusive",
    budget=lambda: config.AVITO_SESSION_USES,
    max_age=lambda: config.AVITO_SESSION_MAX_AGE,
    on_retire=lambda ident, reason: ident.close())


def _on_session(fn):
    """fn(sess) on a borrowed session. A retryable failure (a block, a
    transport error, a degraded answer) marks the session dead, so it is
    retired on give-back and the retry rides a fresh exit."""
    sess = _sessions.borrow()
    try:
        return fn(sess)
    except AvitoUpstreamError:
        sess.dead = True
        raise
    finally:
        _sessions.give_back(sess)


def _retrying(fn):
    """Run fn() under the shared retry policy (AvitoUpstreamError only;
    NotFound / BadRequest surface immediately). The failed call already
    retired its session (_on_session)."""
    return retry.run(fn, config, AvitoUpstreamError, None)


def _cookie(curl, name):
    """A cookie value by name (the jar may hold the same name for two
    domains; the newest wins)."""
    value = None
    for cookie in curl.cookies.jar:
        if cookie.name == name:
            value = cookie.value
    return value


def _jwt_payload(token):
    part = token.split(".")[1]
    part += "=" * (-len(part) % 4)
    return fastjson.loads(base64.urlsafe_b64decode(part))


def solve_pow(challenge_id, complexity, max_complexity=None):
    """The site's proof-of-work: the first nonce whose sha256("<id>:<nonce>")
    hex digest starts with `complexity` zeros."""
    limit = max_complexity if max_complexity is not None else config.AVITO_MAX_POW_COMPLEXITY
    if complexity > limit:
        raise AvitoBlocked(f"proof-of-work complexity {complexity} above the {limit} we solve")
    prefix = "0" * complexity
    nonce = 0
    seed = f"{challenge_id}:"
    while not hashlib.sha256(f"{seed}{nonce}".encode()).hexdigest().startswith(prefix):
        nonce += 1
    return nonce


def _clear_pow(curl, referer):
    """Answer the 439 proof-of-work on this curl session (raises AvitoBlocked)."""
    challenge = _cookie(curl, "pow_challenge")
    if not challenge:
        raise AvitoBlocked("proof-of-work page without a pow_challenge cookie")
    headers = {"accept": "*/*", "origin": HOST, "referer": referer}
    try:
        got = curl.post(HOST + "/web/3/firewallPow/get", json={"challenge": challenge},
                        headers=headers, timeout=JSON_TIMEOUT).json()
        token = ((got.get("success") or {}).get("result") or {}).get("challenge_jwt")
        payload = _jwt_payload(token)
        nonce = solve_pow(payload["id"], int(payload["compl"]))
        verified = curl.post(HOST + "/web/3/firewallPow/verify",
                             json={"challenge": token, "nonce": nonce},
                             headers=headers, timeout=JSON_TIMEOUT).json()
    except AvitoBlocked:
        raise
    except Exception as e:
        raise AvitoBlocked(f"proof-of-work exchange failed: {type(e).__name__}: {e}")
    result = (verified.get("success") or {}).get("result") or {}
    if not result.get("verified"):
        raise AvitoBlocked(f"proof-of-work not verified: {str(verified)[:120]}")


def _title(text):
    match = _TITLE_RE.search((text or "")[:6000])
    return match.group(1).strip() if match else ""


def _classify(resp, label, kind):
    """Map an HTTP response to the error taxonomy; return the response."""
    status = resp.status_code
    if status == 439:
        raise AvitoBlocked(f"proof-of-work still demanded for {label}")
    if status == 429:
        raise AvitoBlocked(f"IP captcha (HTTP 429) for {label}")
    if status in (401, 403):
        raise AvitoBlocked(f"HTTP {status} for {label}")
    if status == 404:
        raise AvitoNotFound(f"{label} not found")
    if status == 400:
        raise AvitoBadRequest(f"HTTP 400 for {label}: {resp.text[:120]}")
    if status != 200:
        raise AvitoUpstreamError(f"HTTP {status} for {label}")
    if kind == "page":
        title = _title(resp.text)
        if POW_TITLE in title or IP_CAPTCHA_TITLE in title:
            raise AvitoBlocked(f"firewall page ({title}) for {label}")
    return resp


def _request(method, path, *, label, kind="json", query=None, body=None, referer=None,
             timeout=None, check=None):
    """One upstream call under the retry policy. `query` is a list of
    (key, value) pairs (repeated keys allowed, e.g. params[1][]=a).
    `check(resp, label)` runs inside the retry loop: it raises AvitoNotFound
    on the site's own not-found signal and AvitoUpstreamError when the
    answer lacks the state the surface promises (retried on a fresh exit)."""
    url = HOST + path
    if query:
        url += ("&" if "?" in url else "?") + urlencode(query)
    headers = dict(PAGE_HEADERS if kind == "page" else JSON_HEADERS)
    headers["referer"] = referer or (HOST + "/")
    if body is not None:
        headers["content-type"] = "application/json"
        headers["origin"] = HOST
    timeout = timeout or (PAGE_TIMEOUT if kind == "page" else JSON_TIMEOUT)

    def send(curl):
        return curl.request(method, url, json=body, headers=headers, timeout=timeout,
                            allow_redirects=True)

    def once(sess):
        try:
            resp = send(sess.curl)
            if resp.status_code == 439:
                _clear_pow(sess.curl, url)      # a fresh session, or a clearance past its ttl
                resp = send(sess.curl)
        except AvitoUpstreamError:
            raise
        except Exception as e:
            raise AvitoUpstreamError(f"request failed: {type(e).__name__}: {e}")
        resp = _classify(resp, label, kind)
        if check is not None:
            check(resp, label)
        return resp

    return _retrying(lambda: _on_session(once))


def _expect(marker, what):
    """A check: the body must contain `marker` (a key of the promised state)."""
    def check(resp, label):
        if marker not in resp.text:
            raise AvitoUpstreamError(f"{label}: answer without {what}")
    return check


def _check_item(resp, label):
    if urlparse(str(resp.url or "")).path.startswith("/not-found"):
        raise AvitoNotFound(f"{label} not found")
    if "buyerItem" not in resp.text:
        raise AvitoUpstreamError(f"{label}: page without the ad state")


def _check_seller(resp, label):
    if _SELLER_NOT_FOUND_RE.search(resp.text):
        raise AvitoNotFound(f"{label} not found")
    if not _SELLER_OK_RE.search(resp.text):
        raise AvitoUpstreamError(f"{label}: profile page rendered without the profile (site-side degradation)")


def _json(resp, label):
    try:
        return resp.json()
    except Exception:
        raise AvitoBlocked(f"non-JSON answer for {label}: {resp.text[:120]!r}")


# ---- surfaces -----------------------------------------------------------------------------

def fetch_search(query, page):
    """GET /web/1/js/items with the given (key, value) pairs -> JSON dict.
    A page past the site's last one answers 400 -> None."""
    label = "search"
    try:
        resp = _request("GET", "/web/1/js/items", label=label, query=list(query) + [("page", page)],
                        referer=HOST + "/all", check=_expect('"catalog":', "the catalog"))
    except AvitoBadRequest:
        if page > 1:
            return None
        raise
    return _json(resp, label)


def fetch_item(item_id):
    """GET /<id> -> (html, final url). /<id> redirects to the canonical ad
    page; an unknown id redirects to /not-found (HTTP 200)."""
    label = f"ad {item_id}"
    resp = _request("GET", f"/{item_id}", label=label, kind="page", check=_check_item)
    return resp.text, str(resp.url or "")


def fetch_seller_page(ref):
    """GET a public profile page for a resolved seller ref (kind brand / key)."""
    path = refs.seller_page_path(ref)
    label = f"seller {ref['id']}"
    resp = _request("GET", path, label=label, kind="page", check=_check_seller)
    return resp.text


def _check_seller_ads(resp, label):
    # a key that names no profile answers {"status":"incorrect-data"} (200)
    if '"catalog":' not in resp.text and '"incorrect-data"' not in resp.text:
        raise AvitoUpstreamError(f"{label}: answer without the catalog")


def fetch_seller_ads(user_key, page, limit, sort=None, category_id=None):
    """GET /web/1/profile/items -> JSON (the profile's active ads), or
    {"status": "incorrect-data"} for a key that names no profile."""
    query = [("categoryId", category_id or 0), ("sellerId", user_key), ("s", sort or "101"),
             ("page", page), ("limit", limit), ("noProfileCategoryTree", "true"),
             ("shortcut", "active")]
    label = f"seller {user_key} ads"
    try:
        resp = _request("GET", "/web/1/profile/items", label=label, query=query,
                        referer=f"{HOST}/user/{user_key}/profile", check=_check_seller_ads)
    except AvitoBadRequest:
        if page > 1:
            return None
        raise
    return _json(resp, label)


def fetch_seller_reviews(user_key, offset, limit, sort, photo_only=False):
    """GET /web/7/user/<key>/ratings -> JSON {entries, nextPage}."""
    query = [("limit", limit), ("offset", offset), ("photoOnly", "true" if photo_only else "false"),
             ("sortRating", sort)]
    label = f"seller {user_key} reviews"
    resp = _request("GET", f"/web/7/user/{user_key}/ratings", label=label, query=query,
                    referer=f"{HOST}/user/{user_key}/profile", check=_expect('"entries":', "the review entries"))
    return _json(resp, label)


def fetch_locations(text, limit, location_id=None):
    """GET /web/1/slocations -> JSON {result: {locations: [...]}}."""
    query = [("limit", limit), ("q", text)]
    if location_id:
        query.insert(0, ("locationId", location_id))
    resp = _request("GET", "/web/1/slocations", label="location search", query=query,
                    check=_expect('"result":', "the location list"))
    return _json(resp, "location search")


def fetch_category_tree_page():
    """GET /not-found: a ~300 KB page whose header state carries the whole
    category navigation tree (the same tree every page ships)."""
    resp = _request("GET", "/not-found", label="category tree", kind="page",
                    check=_expect("categoryTree", "the category tree"))
    return resp.text


def fetch_location_page(slug):
    """GET /<slug> (a location's home page) -> html, read once for its id."""
    resp = _request("GET", f"/{slug}", label=f"location {slug}", kind="page")
    if urlparse(str(resp.url or "")).path.startswith("/not-found"):
        raise AvitoNotFound(f"location {slug} not found")
    return resp.text
