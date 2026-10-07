"""Measure real outcomes; idle time/restarts and changing inputs affect the report."""
from datetime import datetime, timedelta, timezone

import requests

from notifier.buff import BuffError, LoginRequired, retry_after_seconds
from notifier.envfile import write_env
from notifier.store import Store
from tests.test_poller import env, page, poller, Client, NOW


def test_polling_records_success_limits_and_expiry_but_not_idle_cycles(env):
    store, _, envp = env
    client = Client(page(), BuffError("429", 429, 600), LoginRequired("expired", 200))
    p = poller(env, client)
    p.cycle(NOW)
    p.cycle(NOW + timedelta(minutes=1))  # item is not due
    p.cycle(NOW + timedelta(hours=4))
    p.cycle(NOW + timedelta(hours=4, minutes=5))  # cooldown
    p.cycle(NOW + timedelta(hours=4, minutes=11))
    p.cycle(NOW + timedelta(hours=4, minutes=12))  # expired session
    m = store.measurement_stats(NOW + timedelta(hours=4, minutes=12))["current"]
    assert (m["attempts"], m["poll_ok"], m["limited"], m["errors"]) == (3, 1, 1, 1)
    assert m["before_429"] == 1 and m["first_429_minutes"] == 240
    limit = store.measurement_stats()["limits"][0]
    assert limit["retry_after"] == 600 and limit["pause_seconds"] == 600
    # A new session starts a separate run instead of mixing the results.
    write_env(envp, {"BUFF_COOKIE": "session=other-secret"})
    client.answers.append(page())
    p.cycle(NOW + timedelta(hours=5))
    stats = store.measurement_stats(NOW + timedelta(hours=5))
    assert stats["current"]["attempts"] == 1 and len(stats["history"]) == 2
    assert "other-secret" not in str(stats)


def test_searches_and_network_failures_are_counted(env):
    class SearchClient:
        def search_goods(self, query):
            return {"code": "OK", "data": {"items": []}}

        def sell_orders(self, goods_id, page_size):
            raise requests.Timeout("secret should not be recorded")

    store, _, _ = env
    store.set_active(5777, False)
    store.add_pending(["M4A4 | Temukau (Field-Tested)"])
    p = poller(env, SearchClient())
    p.cycle(NOW)
    store.set_active(5777, True)
    p.cycle(NOW + timedelta(seconds=5))
    stats = store.measurement_stats(NOW + timedelta(seconds=10))
    assert stats["current"]["search_ok"] == 1
    assert stats["current"]["errors"] == 1 and stats["current"]["attempts"] == 2
    assert "secret" not in str(stats)


def test_runs_survive_restarts_and_separate_changes(tmp_path):
    path = tmp_path / "buff.db"
    store = Store(path)
    mid = store.measurement("digest1", 5, False, NOW)
    store.record_request(mid, NOW, "poll", "ok", 200)
    store.conn.close()
    store = Store(path)
    assert store.measurement("digest1", 5, False, NOW + timedelta(minutes=10)) == mid
    second = store.measurement("digest2", 10, True, NOW + timedelta(minutes=20))
    assert second != mid
    # Returning to older settings creates a fresh run, not a misleading merged one.
    third = store.measurement("digest1", 5, False, NOW + timedelta(minutes=30))
    assert third not in (mid, second)


def test_hour_counts_include_idle_and_pruning_keeps_cumulative_totals(tmp_path):
    store = Store(tmp_path / "buff.db")
    mid = store.measurement("digest", 5, False, NOW)
    store.record_request(mid, NOW, "poll", "ok", 200)
    later = NOW + timedelta(hours=2)
    store.record_request(mid, later, "search", "ok", 200)
    store.record_request(mid, later + timedelta(seconds=5), "poll", "limited", 429, None, 900)
    stats = store.measurement_stats(later + timedelta(minutes=10))
    assert stats["current"]["hour"] == {"attempts": 2, "polls": 0, "searches": 1, "limited": 1}
    store.prune_requests(NOW + timedelta(days=8))
    stats = store.measurement_stats(NOW + timedelta(days=8))
    assert stats["current"]["attempts"] == 3 and stats["current"]["before_429"] == 2
    assert stats["current"]["hour"]["attempts"] == 0 and stats["limits"] == []


def test_retry_after_handles_both_formats_and_invalid_values():
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    assert retry_after_seconds("Thu, 08 Oct 2026 00:10:00 GMT", now) == 600
    assert retry_after_seconds("Thu, 08 Oct 2026 00:00:00 GMT", now) == 0
    assert retry_after_seconds("60") == 60
    for value in (None, "bad", "nan", "inf", "-1"):
        assert retry_after_seconds(value, now) is None
