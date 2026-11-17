"""Runs a traffic mix against the shop's frontend (and internal jobs against services)."""

from __future__ import annotations

import asyncio
import contextlib
import http.cookiejar
import json
import logging
import time
from collections.abc import Iterator
from typing import Any

import httpx

from harness.loadgen.mixes import Mix, get_mix
from harness.loadgen.schedule import Event, stream_events, user_events
from harness.loadgen.stats import Stats

logger = logging.getLogger("loadgen")

SESSION_COOKIE = "shop_session"
STATS_INTERVAL_S = 10.0


class _NoCookies(http.cookiejar.CookieJar):
    """Each simulated user carries its own session; the shared client must not."""

    def set_cookie(self, cookie: http.cookiejar.Cookie) -> None:
        return None

    def extract_cookies(self, response: Any, request: Any) -> None:
        return None


class LoadGenerator:
    def __init__(
        self,
        *,
        target_url: str,
        order_service_url: str,
        seed: int,
        mix: str = "baseline",
        max_in_flight: int = 200,
        timeout_s: float = 10.0,
    ) -> None:
        self.seed = seed
        self.mix: Mix = get_mix(mix)
        self.max_in_flight = max_in_flight
        self.stats = Stats()
        self.in_flight = 0
        self.mix_started_at = time.time()
        self._sessions: dict[int, str] = {}
        self._schedulers: list[asyncio.Task[None]] = []
        self._requests: set[asyncio.Task[None]] = set()
        self.shop = httpx.AsyncClient(
            base_url=target_url,
            timeout=timeout_s,
            follow_redirects=False,
            cookies=httpx.Cookies(_NoCookies()),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=50),
        )
        self.internal = httpx.AsyncClient(base_url=order_service_url, timeout=timeout_s * 3)

    # ---- mix control ---------------------------------------------------------------------

    def set_mix(self, name: str) -> Mix:
        mix = get_mix(name)  # raises KeyError for unknown mixes
        for task in self._schedulers:
            task.cancel()
        self.mix = mix
        self.mix_started_at = time.time()
        start = time.monotonic()
        self._schedulers = [
            asyncio.create_task(self._drive(user_events(self.seed, mix), start))
        ] + [
            asyncio.create_task(self._drive(stream_events(self.seed, mix, s), start))
            for s in mix.streams
        ]
        logger.info(json.dumps({"event": "mix", "mix": mix.name, "seed": self.seed}))
        return mix

    async def _drive(self, events: Iterator[Event], start: float) -> None:
        for event in events:
            delay = start + event.at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            if self.in_flight >= self.max_in_flight:
                self.stats.dropped += 1
                continue
            task = asyncio.create_task(self._execute(event))
            self._requests.add(task)
            task.add_done_callback(self._requests.discard)

    # ---- actions -------------------------------------------------------------------------

    async def _execute(self, event: Event) -> None:
        self.in_flight += 1
        started = time.perf_counter()
        error = declined = False
        try:
            status = await self._perform(event)
            error = status >= 500
            declined = status == 402
        except httpx.HTTPError:
            error = True
        finally:
            self.in_flight -= 1
            self.stats.record(
                event.action, (time.perf_counter() - started) * 1000, error=error, declined=declined
            )

    async def _perform(self, ev: Event) -> int:
        if ev.action == "browse":
            if ev.target == "home":
                return (await self.shop.get("/")).status_code
            return (await self.shop.get("/products", params={"category": ev.target})).status_code
        if ev.action == "product":
            return (await self.shop.get(f"/products/{ev.target}")).status_code
        if ev.action == "login":
            return await self._login(ev.user)
        if ev.action == "account":
            return await self._signed_in(ev.user, lambda h: self.shop.get("/account", headers=h))
        if ev.action == "checkout":
            data = {"sku": ev.target, "qty": str(ev.qty)}
            return await self._signed_in(
                ev.user, lambda h: self.shop.post("/checkout", data=data, headers=h)
            )
        if ev.action == "catalogue_nonce":
            category, nonce = ev.target.split(":", 1)
            params = {"category": category, "v": nonce}
            return (await self.shop.get("/products", params=params)).status_code
        if ev.action == "internal_report":
            params = {"days": ev.target}
            return (await self.internal.get("/internal/reports/sales", params=params)).status_code
        raise ValueError(f"unknown action {ev.action}")

    async def _login(self, user: int) -> int:
        resp = await self.shop.post(
            "/login", data={"email": f"user{user}@shop.test", "password": f"pw-{user}"}
        )
        token = resp.cookies.get(SESSION_COOKIE)
        if token:
            self._sessions[user] = token
        return resp.status_code

    async def _signed_in(self, user: int, call: Any) -> int:
        if user not in self._sessions:
            status = await self._login(user)
            if user not in self._sessions:
                return int(status)
        resp: httpx.Response = await call({"Cookie": f"{SESSION_COOKIE}={self._sessions[user]}"})
        if resp.status_code == 303 and resp.headers.get("location") == "/login":
            self._sessions.pop(user, None)  # session expired: sign in again once
            await self._login(user)
            if user in self._sessions:
                cookie = {"Cookie": f"{SESSION_COOKIE}={self._sessions[user]}"}
                resp = await call(cookie)
        return resp.status_code

    # ---- lifecycle -----------------------------------------------------------------------

    async def report_forever(self) -> None:
        while True:
            await asyncio.sleep(STATS_INTERVAL_S)
            summary = self.stats.roll()
            summary.update(
                event="stats",
                mix=self.mix.name,
                in_flight=self.in_flight,
                dropped_total=self.stats.dropped,
            )
            logger.info(json.dumps(summary))

    def snapshot(self) -> dict[str, Any]:
        return {
            "mix": self.mix.name,
            "seed": self.seed,
            "mix_started_at": self.mix_started_at,
            "in_flight": self.in_flight,
            "dropped": self.stats.dropped,
            "totals": self.stats.totals.summary(),
        }

    async def close(self) -> None:
        for task in [*self._schedulers, *self._requests]:
            task.cancel()
        with contextlib.suppress(Exception):
            await self.shop.aclose()
            await self.internal.aclose()
