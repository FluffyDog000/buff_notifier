"""The request is the item page's own, sent one at a time."""
import pytest

from notifier.buff import API, SELL_ORDER, BuffClient, BuffError, LoginRequired


class Resp:
    def __init__(self, status=200, headers=None, body=None):
        self.status_code, self.headers = status, headers or {}
        self.body = {"code": "OK", "data": {}} if body is None else body
        self.text = str(self.body)

    def json(self):
        return self.body


class Session:
    def __init__(self, *responses):
        self.headers, self.calls, self.responses = {}, [], list(responses)

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return self.responses.pop(0) if self.responses else Resp()


class Clock:
    def __init__(self):
        self.t, self.slept = 100.0, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def test_the_page_request_is_rebuilt_with_the_accounts_session():
    s = Session()
    body = BuffClient(cookie="session=abc; client_id=x", csrf="tok", user_agent="UA",
                      session=s).sell_orders(5777)
    assert body["code"] == "OK"
    url, params = s.calls[0]
    assert url == API + SELL_ORDER
    assert params == {"game": "csgo", "page_num": 1, "page_size": 10,
                      "goods_id": 5777, "sort_by": "created.desc"}
    assert s.headers["Cookie"] == "session=abc; client_id=x"
    assert s.headers["X-CSRFToken"] == "tok" and s.headers["User-Agent"] == "UA"


def test_without_a_session_nothing_of_one_is_sent():
    s = Session()
    BuffClient(session=s).sell_orders(1)
    assert not {k.lower() for k in s.headers} & {"cookie", "x-csrftoken"}


def test_login_required_is_told_apart_from_other_refusals():
    login = {"code": "Login Required", "error": "Please login.", "extra": None}
    with pytest.raises(LoginRequired):
        BuffClient(session=Session(Resp(body=login))).sell_orders(1)
    with pytest.raises(BuffError) as e:
        BuffClient(session=Session(Resp(body={"code": "Bad", "error": "x"}))).sell_orders(1)
    assert not isinstance(e.value, LoginRequired) and e.value.code == "Bad"


def test_requests_keep_their_distance():
    clock = Clock()
    c = BuffClient(min_interval=5, session=Session(), clock=clock, sleep=clock.sleep)
    c.sell_orders(1)
    clock.t += 2
    c.sell_orders(2)
    assert clock.slept == [pytest.approx(3.0)]


def test_a_429_says_so_with_its_wait():
    c = BuffClient(session=Session(Resp(429, {"Retry-After": "60"})))
    with pytest.raises(BuffError) as e:
        c.sell_orders(1)
    assert e.value.status == 429 and e.value.retry_after == 60.0
