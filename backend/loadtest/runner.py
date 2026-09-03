"""The HTTP-mix run loop: virtual users, weighted actions, pacing, abort watch."""
from __future__ import annotations

import asyncio
import random
import time

import httpx

from loadtest.abort import AbortMonitor, Violation
from loadtest.accounts import Manifest, VirtualUser, auth_headers
from loadtest.config import HarnessConfig
from loadtest.metrics import REFERENCE_CLASS, WRITE_CLASSES, Recorder, Sample
from loadtest.pacing import Pacer

# Enough comment targets that random picks spread; small enough that warmup
# stays a trickle even on an empty database.
MIN_WARMUP_POSTS = 5
ABORT_CHECK_INTERVAL_SECONDS = 2.0
REQUEST_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
PROBE_INTERVAL_SECONDS = 1.0


def ramp_delay(index: int, total: int, ramp_seconds: float) -> float:
    """When user `index` of `total` starts, spread evenly across the ramp (c285).

    User 0 starts immediately; the last user starts at ramp_seconds. With one
    user or no ramp, everyone starts at 0 - the pre-c285 behavior.
    """
    if total <= 1 or ramp_seconds <= 0:
        return 0.0
    return ramp_seconds * index / (total - 1)


class TargetPool:
    """Shared pool of post ids for comment reads/writes, harvested from list
    responses as the run goes, so targets stay fresh without a metadata store."""

    def __init__(self) -> None:
        self._post_ids: list[str] = []

    def add_posts(self, ids: list[str]) -> None:
        merged = set(self._post_ids)
        merged.update(ids)
        # Bounded so an hour-long run does not grow this without limit.
        self._post_ids = list(merged)[-500:]

    def random_post(self) -> str | None:
        if not self._post_ids:
            return None
        return random.choice(self._post_ids)


class Runner:
    def __init__(self, config: HarnessConfig, manifest: Manifest) -> None:
        self.config = config
        self.manifest = manifest
        self.recorder = Recorder(config.abort.window_seconds)
        self.pacer = Pacer(
            config.caps.max_rps,
            config.caps.max_concurrent_requests,
            config.caps.per_user_writes_per_minute,
        )
        self.monitor = AbortMonitor(config.abort, self.recorder)
        self.targets = TargetPool()
        self.stop = asyncio.Event()
        self.abort_violations: list[Violation] = []
        self._t0 = time.monotonic()

    def _now(self) -> float:
        return time.monotonic() - self._t0

    # ---- single request ----

    async def _request(
        self,
        client: httpx.AsyncClient,
        user: VirtualUser,
        route_class: str,
        method: str,
        path: str,
        json_body: dict | None = None,
    ) -> httpx.Response | None:
        await self.pacer.global_bucket.acquire()
        async with self.pacer.semaphore:
            start = time.monotonic()
            try:
                response = await client.request(
                    method,
                    path,
                    json=json_body,
                    headers=auth_headers(user, self.config.auth_mode),
                )
                status = response.status_code
            except httpx.HTTPError:
                response = None
                status = 0
            latency_ms = (time.monotonic() - start) * 1000
        self.recorder.record(
            Sample(at=self._now(), route_class=route_class, status=status, latency_ms=latency_ms)
        )
        return response

    # ---- the route classes ----

    async def _do_action(self, client: httpx.AsyncClient, user: VirtualUser, route_class: str) -> None:
        campus = self.manifest.campus_id
        chapter = self.manifest.chapter_id
        if route_class == "feed_campus":
            await self._request(client, user, route_class, "GET", f"/campuses/{campus}/feed")
        elif route_class == "chirps_list":
            await self._request(client, user, route_class, "GET", f"/campuses/{campus}/chirps")
        elif route_class == "posts_list":
            response = await self._request(
                client, user, route_class, "GET", f"/chapters/{chapter}/posts"
            )
            self._harvest_posts(response)
        elif route_class == "comments_list":
            post_id = self.targets.random_post()
            if post_id is None:
                await self._request(client, user, "posts_list", "GET", f"/chapters/{chapter}/posts")
                return
            await self._request(client, user, route_class, "GET", f"/posts/{post_id}/comments")
        elif route_class == "me":
            await self._request(client, user, route_class, "GET", "/auth/me")
        elif route_class == "post_create":
            response = await self._request(
                client,
                user,
                route_class,
                "POST",
                f"/chapters/{chapter}/posts",
                json_body={"body": _text("post", user.uid)},
            )
            self._harvest_created(response)
        elif route_class == "comment_create":
            post_id = self.targets.random_post()
            if post_id is None:
                return
            await self._request(
                client,
                user,
                route_class,
                "POST",
                f"/posts/{post_id}/comments",
                json_body={"body": _text("comment", user.uid)},
            )
        elif route_class == "chirp_create":
            await self._request(
                client,
                user,
                route_class,
                "POST",
                f"/campuses/{campus}/chirps",
                json_body={"body": _text("chirp", user.uid)},
            )
        else:
            raise SystemExit(f"mix_weights names unknown route class {route_class!r}")

    def _harvest_posts(self, response: httpx.Response | None) -> None:
        if response is None or response.status_code != 200:
            return
        try:
            items = response.json()
        except ValueError:
            return
        if isinstance(items, list):
            ids = [str(item["id"]) for item in items if isinstance(item, dict) and "id" in item]
            self.targets.add_posts(ids)

    def _harvest_created(self, response: httpx.Response | None) -> None:
        if response is None or response.status_code != 201:
            return
        try:
            body = response.json()
        except ValueError:
            return
        if isinstance(body, dict) and "id" in body:
            self.targets.add_posts([str(body["id"])])

    # ---- virtual user loop ----

    async def _user_loop(
        self, client: httpx.AsyncClient, user: VirtualUser, start_delay: float = 0.0
    ) -> None:
        if start_delay > 0:
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=start_delay)
                return
            except asyncio.TimeoutError:
                pass
        classes = list(self.config.mix_weights.keys())
        weights = list(self.config.mix_weights.values())
        read_classes = [c for c in classes if c not in WRITE_CLASSES]
        read_weights = [self.config.mix_weights[c] for c in read_classes]
        while not self.stop.is_set():
            think = self.config.think_seconds * random.uniform(0.5, 1.5)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=max(think, 0.01))
                return
            except asyncio.TimeoutError:
                pass
            route_class = random.choices(classes, weights=weights, k=1)[0]
            if route_class in WRITE_CLASSES and not self.pacer.write_allowed(user.uid, route_class):
                self.recorder.record_substituted_write()
                if not read_classes:
                    continue
                route_class = random.choices(read_classes, weights=read_weights, k=1)[0]
            await self._do_action(client, user, route_class)

    # ---- warmup ----

    async def _warmup(self, client: httpx.AsyncClient) -> None:
        """Make sure comment targets exist: read first, create the shortfall.

        Warmup writes go through the same pacer as everything else — warmup is
        not a licence to burst.
        """
        first = self.manifest.users[0]
        response = await self._request(
            client, first, "posts_list", "GET", f"/chapters/{self.manifest.chapter_id}/posts"
        )
        self._harvest_posts(response)
        creators = self.manifest.users[: MIN_WARMUP_POSTS * 2]
        index = 0
        while len(self.targets._post_ids) < MIN_WARMUP_POSTS and index < len(creators):
            user = creators[index]
            index += 1
            if not self.pacer.write_allowed(user.uid, "post_create"):
                continue
            response = await self._request(
                client,
                user,
                "post_create",
                "POST",
                f"/chapters/{self.manifest.chapter_id}/posts",
                json_body={"body": _text("warmup post", user.uid)},
            )
            self._harvest_created(response)

    # ---- abort watch ----

    async def _abort_watch(self) -> None:
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=ABORT_CHECK_INTERVAL_SECONDS)
                return
            except asyncio.TimeoutError:
                pass
            violations = self.monitor.check(self._now())
            if violations:
                self.abort_violations = violations
                self.stop.set()
                return

    # ---- entry ----

    async def run_http_phase(self) -> None:
        limits = httpx.Limits(
            max_connections=self.config.caps.max_concurrent_requests + 5,
            max_keepalive_connections=self.config.caps.max_concurrent_requests,
        )
        async with httpx.AsyncClient(
            base_url=self.config.base_url, timeout=REQUEST_TIMEOUT, limits=limits
        ) as client:
            await self._warmup(client)
            watch = asyncio.create_task(self._abort_watch())
            probe = asyncio.create_task(self._reference_probe())
            total = len(self.manifest.users)
            users = [
                asyncio.create_task(
                    self._user_loop(
                        client,
                        user,
                        ramp_delay(index, total, self.config.ramp_in_seconds),
                    )
                )
                for index, user in enumerate(self.manifest.users)
            ]
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=self.config.duration_seconds)
            except asyncio.TimeoutError:
                pass
            self.stop.set()
            await asyncio.gather(*users, return_exceptions=True)
            watch.cancel()
            probe.cancel()
            await asyncio.gather(watch, probe, return_exceptions=True)

    async def _reference_probe(self) -> None:
        """The instrument's self-audit (c285): one request per second on its OWN
        client and connection, outside every cap and semaphore, recorded under
        REFERENCE_CLASS. Its p95 approximates what the server actually did; the
        report compares the mix against it and calls out driver saturation.

        Deliberately outside the pacer: queueing the probe behind the mix would
        make it measure the same contention it exists to expose. Cost: 1 rps.
        """
        user = self.manifest.users[0]
        async with httpx.AsyncClient(
            base_url=self.config.base_url,
            timeout=REQUEST_TIMEOUT,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        ) as probe_client:
            while not self.stop.is_set():
                start = time.monotonic()
                try:
                    response = await probe_client.get(
                        "/auth/me", headers=auth_headers(user, self.config.auth_mode)
                    )
                    status = response.status_code
                except httpx.HTTPError:
                    status = 0
                self.recorder.record(
                    Sample(
                        at=self._now(),
                        route_class=REFERENCE_CLASS,
                        status=status,
                        latency_ms=(time.monotonic() - start) * 1000,
                    )
                )
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=PROBE_INTERVAL_SECONDS)
                    return
                except asyncio.TimeoutError:
                    continue


def _text(kind: str, uid: str) -> str:
    """Distinctive, greppable synthetic content — a cleanup query can find every
    row this harness ever wrote (LOADTEST marker plus the writing uid)."""
    stamp = int(time.time() * 1000) % 1_000_000
    return f"LOADTEST {kind} from {uid} seq {stamp} - synthetic content, safe to delete"
