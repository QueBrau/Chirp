"""Harness configuration: load shape, hard rate caps, and MANDATORY abort criteria."""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

import yaml

# The c259 production limits the harness must stay under, copied here as the
# denominators for the per-user write caps below. If c259's numbers change,
# these change with them — the config loader refuses a cap that exceeds the
# limit it is derived from, so a drift here fails loudly instead of 429-storming.
# Format: scope -> (max_calls, window_seconds), from app/core/rate_limits.py.
C259_LIMITS: dict[str, tuple[int, int]] = {
    "post_create": (60, 600),
    "comment_create": (60, 600),
    "chirp_create": (30, 600),
}

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


class ConfigError(SystemExit):
    """Raised (and exits non-zero) on any config problem — a misconfigured load
    test must never 'run anyway'."""


def _is_local(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return host in LOCAL_HOSTS


@dataclass(frozen=True)
class AbortCriteria:
    """Written BEFORE the harness can run — every field is required, no defaults.

    The absence of defaults is the point: c226's instruction is that abort
    criteria exist before a single request is sent, so a config file that does
    not spell them out refuses to load rather than inheriting someone's guess.
    """

    # 5xx + transport errors as a percentage of requests in the rolling window.
    max_error_rate_pct: float
    # 429s tracked SEPARATELY from errors: at paced rates they should be ~0, and
    # a rising 429 rate means the harness (or the limiter) is not behaving as
    # modelled — that is a stop-and-look signal, not a capacity finding.
    max_429_rate_pct: float
    # Rolling p95 ceilings, split read/write because their baselines differ.
    read_p95_ceiling_ms: float
    write_p95_ceiling_ms: float
    # WS storm: handshake failures (refused, timeout, rejected pre-accept) as a
    # percentage of attempts. Post-accept closes (4401/4403/4503) are reported
    # by close code instead — locally with no Redis every socket 4503s and that
    # is expected, not a connect failure.
    max_ws_failure_pct: float
    # Rolling window all rates/percentiles above are computed over.
    window_seconds: float
    # Evaluation only starts once the window holds this many samples, so the
    # first request of the run cannot trip a percentage criterion by itself.
    min_samples: int
    # No criterion is evaluated before this many seconds have elapsed (c285): a
    # herd of fresh users opening connections inflates the first window with the
    # DRIVER's own costs, and B3 aborted on exactly that. Required, no default -
    # write down how long your ramp needs, like every other criterion here.
    grace_seconds: float

    def validate(self) -> None:
        for name in ("max_error_rate_pct", "max_429_rate_pct", "max_ws_failure_pct"):
            v = getattr(self, name)
            if not 0 <= v <= 100:
                raise ConfigError(f"abort.{name} must be a percentage 0-100, got {v}")
        if self.read_p95_ceiling_ms <= 0 or self.write_p95_ceiling_ms <= 0:
            raise ConfigError("abort p95 ceilings must be positive milliseconds")
        if self.window_seconds < 5:
            raise ConfigError("abort.window_seconds under 5s would flap on noise")
        if self.min_samples < 1:
            raise ConfigError("abort.min_samples must be at least 1")
        if self.grace_seconds < 0:
            raise ConfigError("abort.grace_seconds must be non-negative")


@dataclass(frozen=True)
class RateCaps:
    """Hard bounds the harness enforces on ITSELF, independent of what the
    server would tolerate. The harness must be incapable of exceeding these."""

    # Global ceiling across all virtual users, enforced by one shared token bucket.
    max_rps: float
    # In-flight request ceiling (semaphore) — bounds our own contribution to the
    # c248 arithmetic no matter how slow responses get.
    max_concurrent_requests: int
    # Per-user, per-scope write rates in calls per minute. Each must sit at or
    # under 50% of the corresponding c259 limit so a paced run never competes
    # with the limiter it is supposed to observe at zero.
    per_user_writes_per_minute: dict[str, float] = field(default_factory=dict)

    def validate(self) -> None:
        if self.max_rps <= 0:
            raise ConfigError("caps.max_rps must be positive")
        if self.max_concurrent_requests < 1:
            raise ConfigError("caps.max_concurrent_requests must be at least 1")
        for scope, per_minute in self.per_user_writes_per_minute.items():
            if scope not in C259_LIMITS:
                raise ConfigError(f"caps: unknown write scope {scope!r}")
            max_calls, window = C259_LIMITS[scope]
            limit_per_minute = max_calls / (window / 60)
            if per_minute > limit_per_minute * 0.5:
                raise ConfigError(
                    f"caps.{scope}={per_minute}/min exceeds 50% of the c259 limit "
                    f"({limit_per_minute}/min) — the harness paces under the limiter, "
                    "it does not race it"
                )
        for scope in C259_LIMITS:
            if scope not in self.per_user_writes_per_minute:
                raise ConfigError(f"caps.per_user_writes_per_minute missing {scope!r}")


@dataclass(frozen=True)
class WsLegConfig:
    """Connect-storm leg: ramp, hold, close."""

    max_sockets: int
    connects_per_second: float
    hold_seconds: float

    def validate(self) -> None:
        if self.max_sockets < 1:
            raise ConfigError("ws.max_sockets must be at least 1")
        if self.connects_per_second <= 0:
            raise ConfigError("ws.connects_per_second must be positive")
        if self.hold_seconds < 0:
            raise ConfigError("ws.hold_seconds must be non-negative")


@dataclass(frozen=True)
class HarnessConfig:
    base_url: str
    ws_url: str
    auth_mode: str  # "emulated" | "firebase", mirroring the server's setting
    duration_seconds: float
    # Stagger virtual-user starts across this many seconds (c285). 0 = the old
    # everyone-at-once behavior, which at 176 users is a connection storm that
    # measures the driver, not the server.
    ramp_in_seconds: float
    # Route-class weights for the HTTP mix; relative, not percentages.
    mix_weights: dict[str, float]
    # Mean per-user think time between actions; jittered ±50% at runtime.
    think_seconds: float
    caps: RateCaps
    abort: AbortCriteria
    ws: WsLegConfig
    # Required for any non-local target; irrelevant (and ignored) for localhost.
    approved_by: str = ""
    approved_date: str = ""

    def validate(self, *, confirm_park_lifted: bool = False) -> None:
        if self.auth_mode not in ("emulated", "firebase"):
            raise ConfigError(f"auth_mode must be emulated|firebase, got {self.auth_mode!r}")
        if self.duration_seconds <= 0:
            raise ConfigError("duration_seconds must be positive")
        if self.ramp_in_seconds < 0:
            raise ConfigError("ramp_in_seconds must be non-negative")
        if self.ramp_in_seconds >= self.duration_seconds:
            raise ConfigError("ramp_in_seconds must be shorter than duration_seconds")
        if self.think_seconds < 0:
            raise ConfigError("think_seconds must be non-negative")
        if not self.mix_weights:
            raise ConfigError("mix_weights must name at least one route class")
        for name, w in self.mix_weights.items():
            if w < 0:
                raise ConfigError(f"mix_weights.{name} must be non-negative")
        if sum(self.mix_weights.values()) <= 0:
            raise ConfigError("mix_weights must sum to a positive weight")
        self.caps.validate()
        self.abort.validate()
        self.ws.validate()

        local = _is_local(self.base_url) and _is_local(self.ws_url)
        if not local:
            # THE PARK (c226): no synthetic traffic touches prod. Lifting it takes
            # all three, on purpose: a recorded approver, a recorded date, and a
            # human re-typing a flag at invocation time. None of the three can be
            # committed into a config file ahead of time and forgotten.
            if self.auth_mode == "emulated":
                raise ConfigError(
                    "REFUSING: emulated auth against a non-local target. The server "
                    "ignores X-Debug-Firebase-Uid outside emulated mode; this run "
                    "could only produce 401 noise against a real environment."
                )
            if not (self.approved_by and self.approved_date):
                raise ConfigError(
                    "REFUSING: non-local target without approved_by/approved_date in "
                    "the config. Jose's park on the load test is still in force."
                )
            if not confirm_park_lifted:
                raise ConfigError(
                    "REFUSING: non-local target without --confirm-park-lifted. "
                    "Re-run with the flag only after Jose's explicit GO."
                )


def load_config(path: str, *, confirm_park_lifted: bool = False) -> HarnessConfig:
    """Load and validate a YAML config; any gap is a refusal, never a default."""
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    def section(name: str) -> dict:
        value = raw.get(name)
        if not isinstance(value, dict):
            raise ConfigError(f"config is missing the required '{name}' section")
        return value

    def require(d: dict, key: str, where: str) -> object:
        if key not in d:
            raise ConfigError(f"config is missing required field '{where}.{key}'")
        return d[key]

    abort_raw = section("abort")
    abort = AbortCriteria(
        max_error_rate_pct=float(require(abort_raw, "max_error_rate_pct", "abort")),
        max_429_rate_pct=float(require(abort_raw, "max_429_rate_pct", "abort")),
        read_p95_ceiling_ms=float(require(abort_raw, "read_p95_ceiling_ms", "abort")),
        write_p95_ceiling_ms=float(require(abort_raw, "write_p95_ceiling_ms", "abort")),
        max_ws_failure_pct=float(require(abort_raw, "max_ws_failure_pct", "abort")),
        window_seconds=float(require(abort_raw, "window_seconds", "abort")),
        min_samples=int(require(abort_raw, "min_samples", "abort")),
        grace_seconds=float(require(abort_raw, "grace_seconds", "abort")),
    )
    caps_raw = section("caps")
    caps = RateCaps(
        max_rps=float(require(caps_raw, "max_rps", "caps")),
        max_concurrent_requests=int(require(caps_raw, "max_concurrent_requests", "caps")),
        per_user_writes_per_minute={
            str(k): float(v)
            for k, v in dict(
                require(caps_raw, "per_user_writes_per_minute", "caps")  # type: ignore[arg-type]
            ).items()
        },
    )
    ws_raw = section("ws")
    ws = WsLegConfig(
        max_sockets=int(require(ws_raw, "max_sockets", "ws")),
        connects_per_second=float(require(ws_raw, "connects_per_second", "ws")),
        hold_seconds=float(require(ws_raw, "hold_seconds", "ws")),
    )
    approval = raw.get("approval") or {}
    config = HarnessConfig(
        base_url=str(require(raw, "base_url", "config")),
        ws_url=str(require(raw, "ws_url", "config")),
        auth_mode=str(require(raw, "auth_mode", "config")),
        duration_seconds=float(require(raw, "duration_seconds", "config")),
        ramp_in_seconds=float(require(raw, "ramp_in_seconds", "config")),
        mix_weights={str(k): float(v) for k, v in dict(require(raw, "mix_weights", "config")).items()},  # type: ignore[arg-type]
        think_seconds=float(require(raw, "think_seconds", "config")),
        caps=caps,
        abort=abort,
        ws=ws,
        approved_by=str(approval.get("approved_by", "")),
        approved_date=str(approval.get("date", "")),
    )
    config.validate(confirm_park_lifted=confirm_park_lifted)
    return config
