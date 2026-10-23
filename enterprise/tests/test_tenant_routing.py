import json
import time
from types import SimpleNamespace

import pytest

from vllm_router.tenant_policy import (
    BackendSnapshot,
    PoolPolicy,
    TenantPolicy,
    TenantRoutingError,
    sign_context,
)
from vllm_router.routers.routing_logic import (
    TenantAffinityRouter,
    cleanup_routing_logic,
    get_routing_logic,
)

KEY = b"synthetic-test-key-not-a-production-secret"


@pytest.fixture
def policy():
    return TenantPolicy({"alpha": PoolPolicy(frozenset({"chat"}), 4, 0.8, 2)}, KEY)


def test_signed_tenant_context(policy):
    signature = sign_context(KEY, "alpha", "session-1", 1100)
    assert policy.authenticate(
        "alpha", "session-1", "1100", signature, now=1000
    ).pools == {"chat"}


@pytest.mark.parametrize(
    "tenant,session,expiry,signature",
    [
        ("beta", "session-1", "1100", "bad"),
        ("alpha", "changed", "1100", "bad"),
        ("alpha", "session-1", "999", "bad"),
        ("alpha", "session-1", "1400", "bad"),
        ("alpha", "session-1", "invalid", "bad"),
    ],
)
def test_invalid_context_is_rejected(policy, tenant, session, expiry, signature):
    with pytest.raises(TenantRoutingError):
        policy.authenticate(tenant, session, expiry, signature, now=1000)


def test_signed_unknown_tenant_is_rejected(policy):
    with pytest.raises(TenantRoutingError) as failure:
        policy.authenticate(
            "beta",
            "session-1",
            "1100",
            sign_context(KEY, "beta", "session-1", 1100),
            now=1000,
        )
    assert failure.value.status == 403


def test_pool_does_not_fall_back_to_other_tenants(policy):
    with pytest.raises(TenantRoutingError):
        policy.select(
            "alpha",
            "one",
            policy.policies["alpha"],
            [BackendSnapshot("private", "batch", 0, 0, 0.1)],
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"waiting": 5},
        {"kv_usage": 0.9},
        {"healthy": False},
        {"asleep": True},
        {"running": float("nan")},
    ],
)
def test_capacity_and_health_guards(policy, changes):
    fields = dict(url="busy", pool="chat", running=0, waiting=0, kv_usage=0.1)
    fields.update(changes)
    with pytest.raises(TenantRoutingError):
        policy.select(
            "alpha", "one", policy.policies["alpha"], [BackendSnapshot(**fields)]
        )


def test_affinity_is_stable_and_avoids_overload(policy):
    choices = [
        BackendSnapshot("one", "chat", 0, 0, 0.1),
        BackendSnapshot("two", "chat", 0, 0, 0.1),
    ]
    preferred = policy.select(
        "alpha", "stable-session", policy.policies["alpha"], choices
    )
    assert preferred == policy.select(
        "alpha", "stable-session", policy.policies["alpha"], list(reversed(choices))
    )
    overloaded = [
        BackendSnapshot(c.url, c.pool, 100 if c.url == preferred else 0, 0, 0.1)
        for c in choices
    ]
    assert (
        policy.select("alpha", "stable-session", policy.policies["alpha"], overloaded)
        != preferred
    )


def test_real_router_filters_a_direct_endpoint(tmp_path, monkeypatch):
    cleanup_routing_logic()
    config = tmp_path / "policy.json"
    config.write_text(json.dumps({"tenants": {"alpha": {"pools": ["chat"]}}}))
    monkeypatch.setenv("SERVEMESH_POLICY_FILE", str(config))
    monkeypatch.setenv("SERVEMESH_SIGNING_KEY", KEY.decode())
    router = TenantAffinityRouter()
    assert get_routing_logic() is router
    expiry = int(time.time()) + 120
    request = SimpleNamespace(
        headers={
            "x-servemesh-tenant": "alpha",
            "x-servemesh-session": "one",
            "x-servemesh-expires": str(expiry),
            "x-servemesh-signature": sign_context(KEY, "alpha", "one", expiry),
        }
    )
    endpoint = SimpleNamespace(
        url="wrong-pool", model_label="batch", healthy=True, sleep=False
    )
    stats = SimpleNamespace(
        num_running_requests=0, num_queuing_requests=0, gpu_cache_usage_perc=0.1
    )
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as failure:
        router.route_request([endpoint], {endpoint.url: stats}, {}, request)
    assert failure.value.status_code == 503
    cleanup_routing_logic()
    with pytest.raises(ValueError):
        get_routing_logic()
