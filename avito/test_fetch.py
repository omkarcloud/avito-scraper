"""avito/fetch.py on the pooled sessions: no network (a fake curl session
inside a real fetch._Session; the pool's mint is faked). The site's
proof-of-work exchange is replayed against the real solver.

    python -m pytest avito/test_fetch.py -q
"""
import base64
import json
import os
import sys
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from avito import fetch  # noqa: E402


def jwt(payload):
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"h.{body}.s"


class Resp:
    def __init__(self, status=200, text="", url="https://www.avito.ru/x", payload=None):
        self.status_code, self.text, self.url, self._payload = status, text, url, payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeCurl:
    """Scripted responses; a 439 answer plants the pow_challenge cookie the
    way the site does."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.cookies = SimpleNamespace(jar=[])
        self.closed = False

    def _answer(self, method, url):
        self.calls.append((method, url))
        resp = self.responses.pop(0)
        if resp.status_code == 439:
            self.cookies.jar.append(SimpleNamespace(name="pow_challenge", value="chal", domain=".avito.ru"))
        return resp

    def request(self, method, url, **kw):
        return self._answer(method, url)

    def post(self, url, **kw):
        return self._answer("POST", url)

    def close(self):
        self.closed = True


@pytest.fixture
def pool(monkeypatch):
    monkeypatch.setattr(fetch.config, "MAX_RETRIES", 1)
    monkeypatch.setattr(fetch.time, "sleep", lambda s: None)
    queue, minted = [], []

    def fake_mint(key, hint=None):
        sess = fetch._Session(None)
        sess.curl.close()
        sess.curl = FakeCurl(queue.pop(0) if queue else [])
        minted.append(sess)
        return sess

    p = fetch._sessions
    p.clear()
    p._mint_fn = fake_mint
    p.minted = minted
    p.script = lambda *per_session: queue.extend(list(r) for r in per_session)
    yield p
    p.clear()
    p._mint_fn = fetch._mint


POW_GET = Resp(payload={"success": {"result": {"challenge_jwt": jwt({"id": "abc", "compl": 1})}}})
POW_VERIFY = Resp(payload={"success": {"result": {"verified": True, "unblock_ttl": 600}}})


def locations(n):
    return Resp(text='{"result": {"locations": [%d]}}' % n, payload={"result": {"locations": [n]}})


def test_three_threads_share_one_cleared_session_and_a_captcha_retires_it(pool):
    """Three sequential calls on three different threads ride ONE pooled
    session: the proof-of-work is solved once, on the first call (before:
    once per thread). The IP captcha (429) then retires it (closed, nothing
    idle)."""
    pool.script([Resp(status=439, text="<title>Доступ ограничен: проверка безопасности</title>"), POW_GET, POW_VERIFY,
                 locations(1), locations(2), locations(3), Resp(status=429, text="captcha")])
    results = []
    for _ in range(3):
        t = threading.Thread(target=lambda: results.append(fetch.fetch_locations("Москва", 5)))
        t.start()
        t.join()
    assert [r["result"]["locations"] for r in results] == [[1], [2], [3]]
    assert len(pool.minted) == 1
    sess = pool.minted[0]
    verifies = [u for m, u in sess.curl.calls if u.endswith("/firewallPow/verify")]
    assert len(verifies) == 1 and len(sess.curl.calls) == 6            # 439, get, verify, resend, two more calls
    assert pool.stats()["idle"] == 1 and not sess.closed
    with pytest.raises(fetch.AvitoBlocked):
        fetch.fetch_locations("Москва", 5)
    assert sess.dead and sess.closed and pool.stats()["idle"] == 0


def test_failed_pow_is_a_block_on_that_session(pool):
    pool.script([Resp(status=439, text="pow"), POW_GET,
                 Resp(payload={"success": {"result": {"verified": False}}})])
    with pytest.raises(fetch.AvitoBlocked, match="not verified"):
        fetch.fetch_locations("Москва", 5)
    assert pool.minted[0].closed and pool.stats()["idle"] == 0
