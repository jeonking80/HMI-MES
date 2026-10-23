"""App-local bounded feature probes without caching registration or authorization."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier, Event, Lock
from types import SimpleNamespace

import httpx
from sqlalchemy.exc import OperationalError

from apps.api.analytics_availability import (
    REQUIRED_ROUTES,
    AnalyticsAvailabilityCache,
    analytics_available,
)


class FakeAuth:
    def __init__(self):
        self._origins = {"https://analytics.example.test"}
        self.guest_viewer = False
        self.calls = 0
        self.broken = False
        self.entered = None
        self.release = None
        self.lock = Lock()

    @contextmanager
    def _transaction(self):
        with self.lock:
            self.calls += 1
        if self.entered:
            self.entered.set()
            assert self.release.wait(2)
        if self.broken:
            raise OperationalError("private connection details", {}, Exception("private details"))
        yield self

    def execute(self, statement):
        return self

    def scalars(self):
        return self

    def all(self):
        return ["0018_clock_evidence"]


def app():
    return SimpleNamespace(
        state=SimpleNamespace(analytics_routes_registered=True),
        routes=[SimpleNamespace(path=path, methods={method}) for path, method in REQUIRED_ROUTES],
    )


def test_success_expires_exactly_at_five_seconds_and_apps_are_isolated():
    clock = [0.0]
    first, second = app(), app()
    auth = FakeAuth()
    one = AnalyticsAvailabilityCache(lambda: clock[0])
    two = AnalyticsAvailabilityCache(lambda: clock[0])
    assert analytics_available(first, auth, cache=one)
    assert analytics_available(first, auth, cache=one)
    assert auth.calls == 1
    auth.broken = True
    assert not analytics_available(second, auth, cache=two)
    assert auth.calls == 2
    clock[0] = 4.999
    assert analytics_available(first, auth, cache=one)
    assert auth.calls == 2
    clock[0] = 5.0
    assert not analytics_available(first, auth, cache=one)
    assert auth.calls == 3


def test_db_failure_is_cached_without_leaking_details_and_recovers(caplog):
    clock = [0.0]
    cache = AnalyticsAvailabilityCache(lambda: clock[0])
    auth = FakeAuth()
    auth.broken = True
    application = app()
    assert not analytics_available(application, auth, cache=cache)
    auth.broken = False
    assert not analytics_available(application, auth, cache=cache)
    assert auth.calls == 1
    clock[0] = 5.0
    assert analytics_available(application, auth, cache=cache)
    assert auth.calls == 2
    assert "private" not in caplog.text


def test_registration_and_marker_fail_closed_immediately_without_db_probe():
    application, auth = app(), FakeAuth()
    cache = AnalyticsAvailabilityCache(lambda: 0.0)
    assert analytics_available(application, auth, cache=cache)
    application.state.analytics_routes_registered = False
    assert not analytics_available(application, auth, cache=cache)
    application.state.analytics_routes_registered = True
    removed = application.routes.pop()
    assert not analytics_available(application, auth, cache=cache)
    application.routes.append(removed)
    assert analytics_available(application, auth, cache=cache)
    assert auth.calls == 1


def test_concurrent_expired_refresh_uses_one_probe():
    application, auth = app(), FakeAuth()
    clock = [0.0]
    cache = AnalyticsAvailabilityCache(lambda: clock[0])
    assert analytics_available(application, auth, cache=cache)
    assert auth.calls == 1
    clock[0] = 5.0
    auth.entered, auth.release = Event(), Event()
    barrier = Barrier(8)

    def read():
        barrier.wait(timeout=2)
        return analytics_available(application, auth, cache=cache)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(read) for _ in range(8)]
        assert auth.entered.wait(2)
        assert auth.calls == 2
        auth.release.set()
        assert all(future.result(timeout=2) for future in futures)
    assert auth.calls == 2


async def test_user_app_factories_have_separate_caches_and_preserve_other_flags():
    from apps.api.user_http import UserHTTPConfig, create_user_app

    origin = "https://analytics.example.test"
    auth = FakeAuth()
    configuration = UserHTTPConfig(origin=origin, monotonic=lambda: 0.0)
    first = create_user_app(auth, configuration)
    second = create_user_app(auth, configuration)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=first), base_url=origin
    ) as client:
        response = await client.get("/auth/config")
        assert response.json() == {"guest_viewer": False, "assistant": False, "analytics": True}
        auth.broken = True
        # A different app's failed probe cannot overwrite this app's cached result.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=second), base_url=origin
        ) as other:
            assert (await other.get("/auth/config")).json()["analytics"] is False
        assert (await client.get("/auth/config")).json()["analytics"] is True
    assert auth.calls == 2
