"""Parallelism, persistent claims, shared IP limits and account isolation."""
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from notifier.accounts import Accounts, profile_key, route_keys
from notifier.buff import BuffError, LoginRequired
from notifier.pool import Pool, Worker
from notifier.store import Store
from tests.test_poller import env, NOW, NAME, TG, page, lot  # noqa: F401


def extra(env, ip="2.2.2.2", proxy="http://u:secret@proxy.example:80"):
    store, _, envp = env
    registry = Accounts(envp.parent / "accounts.json", envp)
    aid = registry.save(None, "Второй", {"BUFF_COOKIE": "session=2"}, proxy, 5)
    account = registry.list()[1]
    registry.tested(aid, profile_key(account), ip)
    registry.toggle(aid)
    return registry, aid


def settled(pool):
    for future, _, _ in list(pool.futures.values()):
        future.result(timeout=5)


def test_profile_requires_verification_and_credentials_are_private(env):
    _, _, envp = env
    registry = Accounts(envp.parent / "accounts.json", envp)
    aid = registry.save(None, "Два", {"BUFF_COOKIE": "secret2"}, "http://u:p@host:80", 5)
    with pytest.raises(ValueError, match="Сначала"):
        registry.toggle(aid)
    registry.tested(aid, profile_key(registry.list()[1]), "2.2.2.2")
    registry.toggle(aid)
    assert registry.list()[1]["enabled"]
    registry.save(aid, "Два", proxy="http://u:new@host:80", interval=5)
    assert not registry.list()[1]["enabled"] and "egress_ip" not in registry.list()[1]
    with pytest.raises(ValueError, match="уже используется"):
        registry.save(None, "Копия", {"BUFF_COOKIE": "session=1"})
    with pytest.raises(ValueError, match="Пауза"):
        registry.save(aid, "Два", interval=float("nan"))
    if os.name != "nt":
        assert registry.path.stat().st_mode & 0o777 == 0o600
    registry.path.write_text(json.dumps([{"id": "incomplete"}]))
    with pytest.raises(ValueError, match="повреждён"):
        registry.list()


def test_known_same_ip_and_same_unverified_endpoint_share_a_route():
    profiles = [dict(id="a", BUFF_PROXY="", egress_ip="1.2.3.4"),
                dict(id="b", BUFF_PROXY="http://u:p@x:80", egress_ip="1.2.3.4"),
                dict(id="c", BUFF_PROXY="http://a:b@x:80"),
                dict(id="d", BUFF_PROXY="http://c:d@x:80")]
    routes = route_keys(profiles)
    assert routes["a"] == routes["b"] and routes["c"] == routes["d"]


def test_atomic_claims_across_connections_and_owner_checked_release(env):
    store, _, envp = env
    store.add_watch(6000, NAME)
    other = Store(envp.parent / "buff.db")
    with ThreadPoolExecutor(2) as executor:
        a = executor.submit(store.claim_work, "poll", "pool:a", NOW)
        b = executor.submit(other.claim_work, "poll", "pool:b", NOW)
        assert {a.result()["goods_id"], b.result()["goods_id"]} == {5777, 6000}
    assert store.claim_work("poll", "third", NOW) is None
    gid = str(a.result()["goods_id"])
    other.release_work("poll", gid, "wrong")
    assert store.claim_work("poll", "third", NOW) is None
    store.renew_claims("pool:", NOW + timedelta(seconds=100))
    assert store.claim_work("poll", "third", NOW + timedelta(seconds=121)) is None
    other.release_work("poll", gid, "pool:a")
    assert store.claim_work("poll", "third", NOW + timedelta(seconds=121))["goods_id"] == int(gid)
    store.add_pending(["A", "B"])
    assert store.claim_work("search", "a", NOW)["name"] != other.claim_work("search", "b", NOW)["name"]
    other.conn.close()


def test_route_gate_preserves_busy_state_pacing_and_cooldown_when_ip_learned(env):
    store = env[0]
    assert store.acquire_route("direct", "a", 5.5, NOW)
    store.link_routes("direct", "ip:1.2.3.4")
    assert not store.acquire_route("ip:1.2.3.4", "b", 5.5, NOW + timedelta(seconds=6))
    store.release_route("direct", "a")
    assert not store.acquire_route("ip:1.2.3.4", "b", 5.5, NOW + timedelta(seconds=5.4))
    assert store.acquire_route("ip:1.2.3.4", "b", 5.5, NOW + timedelta(seconds=5.5))
    store.release_route("ip:1.2.3.4", "b")
    store.pause_route("direct", (NOW + timedelta(minutes=15)).isoformat())
    reopened = Store(env[2].parent / "buff.db")
    assert not reopened.acquire_route("ip:1.2.3.4", "c", 5, NOW + timedelta(minutes=10))
    assert reopened.acquire_route("ip:other", "c", 5, NOW + timedelta(minutes=10))
    reopened.conn.close()


def test_separate_accounts_keep_their_own_measurement(env):
    store = env[0]
    a = store.measurement("a", 5, False, NOW, "primary")
    b = store.measurement("b", 5, True, NOW, "second")
    assert store.measurement("a", 5, False, NOW, "primary") == a
    store.record_request(a, NOW, "poll", "ok", 200)
    store.record_request(b, NOW, "poll", "limited", 429, 600, 600)
    assert store.measurement_stats(NOW)["current"]["limited"] == 0
    assert store.measurement_stats(NOW, "second")["current"]["poll_ok"] == 0


def test_two_routes_poll_concurrently_distinct_items_and_alert_once(env):
    store, settings, envp = env
    extra(env)
    store.add_watch(6000, NAME)
    barrier = threading.Barrier(2)
    calls = []

    class Client:
        def sell_orders(self, gid, page_size=10):
            calls.append(gid)
            barrier.wait(timeout=3)
            return page(lot(1, 80, 0.161))

    pool = Pool(envp.parent / "buff.db", settings, envp,
                worker_factory=lambda *args: Worker(*args, client_factory=lambda s, sec: Client(), telegram_factory=TG))
    try:
        pool.tick(NOW)
        settled(pool)
        assert sorted(calls) == [5777, 6000] and len(TG.sent) == 1
        assert len(store.recent_signals()) == 1
        assert all(store.measurement_stats(NOW + timedelta(seconds=1), aid)["current"]["poll_ok"] == 1
                   for aid in ("primary", pool.accounts.list()[1]["id"]))
    finally:
        pool.close()


def test_shared_ip_serializes_requests_and_keeps_total_pace(env):
    store, settings, envp = env
    registry, aid = extra(env, ip="1.1.1.1")
    registry.tested("primary", profile_key(registry.list()[0]), "1.1.1.1")
    calls = []

    class Client:
        def sell_orders(self, gid, page_size=10):
            calls.append(gid)
            return page()

    pool = Pool(envp.parent / "buff.db", settings, envp,
                worker_factory=lambda *args: Worker(*args, client_factory=lambda s, sec: Client(), telegram_factory=TG))
    try:
        pool.tick(NOW)
        settled(pool)
        assert len(calls) == 1
        pool.tick(NOW + timedelta(seconds=4))
        assert len(pool.futures) == 0
        pool.tick(NOW + timedelta(seconds=5))
        settled(pool)
        assert len(calls) == 2
        assert store.measurement_stats(NOW + timedelta(seconds=6), aid)["current"]["poll_ok"] == 1
    finally:
        pool.close()


@pytest.mark.parametrize("failure", [BuffError("limited", 429, 600), LoginRequired("expired")])
def test_limit_or_expired_session_does_not_stop_other_route(env, failure):
    store, settings, envp = env
    _, aid = extra(env)
    store.add_watch(6000, NAME)
    calls = []

    class Client:
        def __init__(self, cookie):
            self.cookie = cookie
        def sell_orders(self, gid, page_size=10):
            calls.append(self.cookie)
            if self.cookie == "session=1":
                raise failure
            return page()

    pool = Pool(envp.parent / "buff.db", settings, envp,
                worker_factory=lambda *args: Worker(*args, client_factory=lambda s, sec: Client(sec["BUFF_COOKIE"]), telegram_factory=TG))
    try:
        pool.tick(NOW)
        settled(pool)
        pool.tick(NOW + timedelta(seconds=5))
        settled(pool)
        assert calls.count("session=1") == 1 and calls.count("session=2") == 2
        assert store.measurement_stats(NOW + timedelta(seconds=6), aid)["current"]["poll_ok"] == 2
        registry = pool.accounts
        registry.toggle(aid)
        pool.tick(NOW + timedelta(seconds=6))
        assert aid not in pool.workers
    finally:
        pool.close()


def test_legacy_primary_cooldown_survives_upgrade_and_constructor_failure_releases_gate(env):
    store, settings, envp = env
    until = (NOW + timedelta(minutes=15)).isoformat()
    store.set_status("pause_until", until)
    pool = Pool(envp.parent / "buff.db", settings, envp,
                worker_factory=lambda *args: (_ for _ in ()).throw(RuntimeError("failed")))
    try:
        pool.tick(NOW)
        assert not pool.futures and store.get_status("account:primary:pause_until") == until
        with pytest.raises(RuntimeError, match="failed"):
            pool.tick(NOW + timedelta(minutes=15))
        row = store.conn.execute("SELECT owner,busy_until FROM routes WHERE key='direct'").fetchone()
        assert row["owner"] is None and row["busy_until"] is None
    finally:
        pool.close()


def test_cleared_expired_marker_stays_cleared_on_restart(env):
    store, settings, envp = env
    store.set_status("session_expired", "stale-before-upgrade")
    store.set_status("account:primary:session_expired", None)
    pool = Pool(envp.parent / "buff.db", settings, envp)
    try:
        assert store.get_status("account:primary:session_expired") is None
    finally:
        pool.close()


def test_worker_writes_queue_before_entering_sqlite(env):
    store, _, envp = env
    other = Store(envp.parent / "buff.db")
    mid = store.measurement("queued-write", 5, False, NOW)
    # With no SQLite retries, the old per-connection locks fail immediately
    # while another worker owns the transaction.
    other.conn.execute("PRAGMA busy_timeout=0")
    started = threading.Event()
    def record():
        started.set()
        other.record_request(mid, NOW, "poll", "ok", 200)
    try:
        with ThreadPoolExecutor(1) as executor:
            with store.lock, store.conn:
                store.conn.execute("BEGIN IMMEDIATE")
                future = executor.submit(record)
                assert started.wait(2)
                with pytest.raises(TimeoutError):
                    future.result(timeout=.05)
            future.result(timeout=2)
        assert store.measurement_stats(NOW)["current"]["poll_ok"] == 1
    finally:
        other.conn.close()
