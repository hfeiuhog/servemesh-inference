"""Signed tenant context and load-bounded affinity for shared inference pools."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path


class TenantRoutingError(ValueError):
    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status


@dataclass(frozen=True)
class PoolPolicy:
    pools: frozenset[str]
    max_waiting: int = 16
    max_kv_usage: float = 0.9
    affinity_skew: float = 2.0


@dataclass(frozen=True)
class BackendSnapshot:
    url: str
    pool: str
    running: float
    waiting: float
    kv_usage: float
    healthy: bool = True
    asleep: bool = False


def sign_context(key: bytes, tenant: str, session: str, expires: int) -> str:
    message = json.dumps([tenant, session, expires], separators=(",", ":")).encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


class TenantPolicy:
    def __init__(self, policies: dict[str, PoolPolicy], key: bytes):
        if len(key) < 32 or not policies:
            raise ValueError(
                "A private key of at least 32 bytes and tenant policies are required"
            )
        self.policies, self.key = policies, key

    @classmethod
    def from_file(cls, path: str, key: bytes) -> TenantPolicy:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if (
            not isinstance(raw, dict)
            or set(raw) != {"tenants"}
            or not isinstance(raw["tenants"], dict)
        ):
            raise ValueError("Expected a tenants mapping")
        result: dict[str, PoolPolicy] = {}
        for tenant, item in raw["tenants"].items():
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", tenant) or not isinstance(
                item, dict
            ):
                raise ValueError("Invalid tenant policy")
            if set(item) - {"pools", "max_waiting", "max_kv_usage", "affinity_skew"}:
                raise ValueError("Unknown tenant policy option")
            pools = item.get("pools")
            waiting, usage, skew = (
                item.get("max_waiting", 16),
                item.get("max_kv_usage", 0.9),
                item.get("affinity_skew", 2),
            )
            if (
                not isinstance(pools, list)
                or not pools
                or not all(
                    isinstance(p, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", p)
                    for p in pools
                )
            ):
                raise ValueError("Pools must contain valid model label names")
            if not isinstance(waiting, int) or isinstance(waiting, bool) or waiting < 0:
                raise ValueError("max_waiting must be a nonnegative integer")
            if (
                not isinstance(usage, (float, int))
                or not math.isfinite(usage)
                or not 0 < usage <= 1
            ):
                raise ValueError("max_kv_usage must be in (0,1]")
            if (
                not isinstance(skew, (float, int))
                or not math.isfinite(skew)
                or skew < 0
            ):
                raise ValueError("affinity_skew must be nonnegative")
            result[tenant] = PoolPolicy(
                frozenset(pools), waiting, float(usage), float(skew)
            )
        return cls(result, key)

    def authenticate(
        self,
        tenant: str,
        session: str,
        expires: str,
        signature: str,
        now: float | None = None,
    ) -> PoolPolicy:
        current = time.time() if now is None else now
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", tenant) or not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,100}", session
        ):
            raise TenantRoutingError(401, "Invalid signed routing context")
        try:
            expiry = int(expires)
        except ValueError as exc:
            raise TenantRoutingError(401, "Invalid routing expiry") from exc
        if not current < expiry <= current + 300:
            raise TenantRoutingError(
                401, "Routing context expired or exceeds five minutes"
            )
        expected = sign_context(self.key, tenant, session, expiry)
        if not hmac.compare_digest(expected, signature):
            raise TenantRoutingError(401, "Routing signature mismatch")
        if tenant not in self.policies:
            raise TenantRoutingError(403, "Tenant has no assigned inference pool")
        return self.policies[tenant]

    def select(
        self,
        tenant: str,
        session: str,
        policy: PoolPolicy,
        backends: list[BackendSnapshot],
    ) -> str:
        eligible: list[tuple[BackendSnapshot, float]] = []
        for item in backends:
            values = (item.running, item.waiting, item.kv_usage)
            if not item.healthy or item.asleep or item.pool not in policy.pools:
                continue
            if not all(math.isfinite(v) and v >= 0 for v in values):
                continue
            if item.waiting > policy.max_waiting or item.kv_usage > policy.max_kv_usage:
                continue
            eligible.append((item, item.running + 2 * item.waiting))
        if not eligible:
            raise TenantRoutingError(
                503, "No eligible inference backend; pool limits remain enforced"
            )
        floor = min(load for _, load in eligible)
        near = [item for item, load in eligible if load <= floor + policy.affinity_skew]

        def rank(item: BackendSnapshot) -> bytes:
            context = json.dumps(
                [tenant, session, item.url], separators=(",", ":")
            ).encode()
            return hmac.new(self.key, context, hashlib.sha256).digest()

        return max(near, key=rank).url
