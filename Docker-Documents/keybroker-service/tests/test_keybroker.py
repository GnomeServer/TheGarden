from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, update

import app as keybroker


@pytest.fixture
def test_client(tmp_path, monkeypatch):
    test_settings = keybroker.settings.model_copy(
        update={
            "database_url": f"sqlite:///{tmp_path / 'keybroker-test.sqlite3'}",
            "proxy_token": SecretStr("proxy-test-token"),
            "dashboard_token": SecretStr("dashboard-test-token"),
            "litellm_master_key": SecretStr("master-test-only"),
            "exposure_webhook_secret": SecretStr("exposure-test-secret"),
            "worker_ip_map": dict(keybroker.WORKER_IPS),
            "default_model": "luna",
            "allowed_models": ["luna"],
            "allow_unpriced_local_model_bases": ["http://llama:8080/v1"],
            "session_max_budget_usd": 25.0,
            "admin_max_budget_usd": 25000.0,
            "session_ttl_seconds": 3600,
            "reaper_interval_seconds": 3600,
            "model_base_url": "https://models.example.test/v1",
        }
    )
    monkeypatch.setattr(keybroker, "settings", test_settings)
    monkeypatch.setattr(
        keybroker,
        "utcnow",
        lambda: datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc),
    )
    test_engine = create_engine(test_settings.sqlalchemy_url(), connect_args={"check_same_thread": False})
    monkeypatch.setattr(keybroker, "engine", test_engine)
    keybroker.MODEL_PRICING_CACHE.clear()
    with TestClient(keybroker.app) as client:
        yield client
    keybroker.MODEL_PRICING_CACHE.clear()
    test_engine.dispose()


def worker_headers(address: str = "100.98.125.127") -> dict[str, str]:
    return {
        "X-Darkfactory-Client-IP": address,
        "X-Darkfactory-Proxy-Token": "proxy-test-token",
    }


def mint(client: TestClient, headers: dict[str, str] | None = None) -> dict[str, str]:
    response = client.post(
        "/v1/sessions",
        headers=headers or worker_headers(),
        json={"run_id": "run-123", "operation": "coding"},
    )
    assert response.status_code == 200
    return response.json()


def install_litellm_stub(monkeypatch, calls: list[tuple[str, str, dict | None]] | None = None):
    request_log = calls if calls is not None else []

    async def fake_request(method, path, *, params=None, body=None, allow_404=False):
        request_log.append((method, path, body))
        if path == "/model/info":
            return {
                "data": [{
                    "model_name": "luna",
                    "litellm_params": {"api_base": "http://llama:8080/v1"},
                    "model_info": {
                        "input_cost_per_token": 0,
                        "output_cost_per_token": 0,
                    },
                }]
            }
        if path == "/key/generate":
            return {"key": "sk-one-time-secret", "token_id": "token-id-123", "token_hash": "safe-hash-456"}
        if path == "/key/list":
            return {
                "keys": [
                    {
                        "token_id": "token-id-123",
                        "token_hash": "safe-hash-456",
                        "key": "sk-must-not-escape",
                        "key_alias": "test-alias",
                        "models": ["luna"],
                        "spend": 1.5,
                        "max_budget": 25,
                        "budget_duration": "30d",
                        "created_at": "2026-10-01T00:00:00Z",
                    }
                ]
            }
        if path == "/spend/logs/v2":
            return {
                "data": [
                    {
                        "startTime": "2026-10-01T00:00:00Z",
                        "spend": 0.5,
                        "prompt_tokens": 10,
                        "completion_tokens": 4,
                        "total_tokens": 14,
                        "api_key": "token-id-123",
                        "model": "gpt-5.6-luna",
                        "prompt": "private user content",
                        "response": "private model output",
                        "raw_key": "sk-must-not-escape",
                    }
                ],
                "total_pages": 3,
                "total_is_capped": False,
            }
        return {"deleted": True}

    monkeypatch.setattr(keybroker, "litellm_request", fake_request)
    return request_log


def test_health_and_readiness(test_client: TestClient):
    assert test_client.get("/healthz").json() == {"status": "ok"}
    assert test_client.get("/readyz").json() == {"status": "ready"}


def test_worker_identity_preflight_requires_trusted_proxy_headers(test_client):
    accepted = test_client.get("/v1/worker/identity", headers=worker_headers())
    assert accepted.status_code == 200
    assert accepted.json() == {"worker_id": "worker-02"}
    for address, worker_id in (
        ("100.70.246.104", "worker-06"),
        ("100.79.110.117", "worker-05"),
    ):
        response = test_client.get(
            "/v1/worker/identity",
            headers=worker_headers(address),
        )
        assert response.status_code == 200
        assert response.json() == {"worker_id": worker_id}
    denied = test_client.get(
        "/v1/worker/identity",
        headers=worker_headers("100.70.246.103"),
    )
    assert denied.status_code == 403

def test_default_enrollment_contains_all_six_requested_workers():
    assert set(keybroker.WORKER_IPS.values()) == {
        "worker-01", "worker-02", "worker-03", "worker-04", "worker-05", "worker-06"
    }
    assert keybroker.WORKER_NAMES["worker-05"] == "sammy"
    assert keybroker.WORKER_NAMES["worker-06"] == "michelangelo"


def test_key_issuance_blocks_unpriced_external_model(test_client, monkeypatch):
    calls: list[str] = []

    async def unpriced_external(method, path, *, params=None, body=None, allow_404=False):
        calls.append(path)
        return {
            "data": [{
                "model_name": "luna",
                "litellm_params": {
                    "model": "openai/custom-luna",
                    "api_base": "https://api.openai.com/v1",
                },
                "model_info": {
                    "input_cost_per_token": 0,
                    "output_cost_per_token": 0,
                },
            }]
        }

    monkeypatch.setattr(keybroker, "litellm_request", unpriced_external)
    worker_result = test_client.post(
        "/v1/sessions",
        headers=worker_headers(),
        json={"run_id": "unpriced-run", "operation": "coding"},
    )
    assert worker_result.status_code == 503
    assert "pricing is missing or zero" in worker_result.json()["detail"]
    admin_result = test_client.post(
        "/v1/admin/keys/generate",
        headers={"Authorization": "Bearer dashboard-test-token"},
        json={"alias": "unpriced-admin", "models": ["luna"], "max_budget_usd": 20},
    )
    assert admin_result.status_code == 503
    assert calls == ["/model/info", "/reload/model_cost_map", "/model/info"]
    with keybroker.Session(keybroker.engine) as session:
        assert session.query(keybroker.SessionRecord).count() == 0

    keybroker.MODEL_PRICING_CACHE.clear()

    async def priced_external(method, path, *, params=None, body=None, allow_404=False):
        return {
            "data": [{
                "model_name": "luna",
                "litellm_params": {"model": "openai/gpt-5.5", "api_base": "https://api.openai.com/v1"},
                "model_info": {
                    "input_cost_per_token": 0.000001,
                    "output_cost_per_token": 0.000005,
                },
            }]
        }

    monkeypatch.setattr(keybroker, "litellm_request", priced_external)
    asyncio.run(keybroker.require_budgetable_model("luna"))


def test_keybroker_reloads_litellm_cost_map_before_blocking_model(test_client, monkeypatch):
    calls: list[str] = []
    refreshed = False

    async def fake_request(method, path, *, params=None, body=None, allow_404=False):
        nonlocal refreshed
        calls.append(path)
        if path == "/reload/model_cost_map":
            refreshed = True
            return {"models_count": 100}
        return {
            "data": [{
                "model_name": "luna",
                "litellm_params": {
                    "model": "openai/gpt-5.6-luna",
                    "api_base": "https://api.openai.com/v1",
                },
                "model_info": {
                    "input_cost_per_token": 0.0000002 if refreshed else 0,
                    "output_cost_per_token": 0.0000012 if refreshed else 0,
                },
            }]
        }

    monkeypatch.setattr(keybroker, "litellm_request", fake_request)
    asyncio.run(keybroker.require_budgetable_model("luna"))
    assert calls == ["/model/info", "/reload/model_cost_map", "/model/info"]

def test_session_policy_is_server_owned_and_raw_key_is_not_persisted(test_client, monkeypatch):
    calls = install_litellm_stub(monkeypatch)
    response = test_client.post(
        "/v1/sessions",
        headers=worker_headers(),
        json={"run_id": "run-123", "operation": "coding"},
    )
    assert response.status_code == 200
    result = response.json()
    assert result["key"] == "sk-one-time-secret"
    assert result["model"] == "luna"
    assert result["key_id"] == "token-id-123"
    assert response.headers["cache-control"] == "no-store"
    generated = next(call[2] for call in calls if call[1] == "/key/generate")
    assert generated["models"] == ["luna"]
    assert generated["max_budget"] == 25
    assert generated["duration"] == "1h"

    record = keybroker.get_session(result["session_id"])
    assert record is not None
    assert record.worker_id == "worker-02"
    assert record.run_id == "run-123"
    assert record.token_id == "token-id-123"
    assert record.token_hash == "safe-hash-456"
    assert record.policy == {
        "model": "luna",
        "max_budget_usd": 25.0,
        "budget_duration": "30d",
        "ttl_seconds": 3600,
    }
    persisted_values = " ".join(
        str(value)
        for value in (
            record.session_id,
            record.worker_id,
            record.run_id,
            record.token_id,
            record.token_hash,
            record.key_alias,
            record.policy,
        )
    )
    assert "sk-one-time-secret" not in persisted_values
    assert "master-test-only" not in persisted_values


def test_duplicate_worker_run_returns_409_and_revokes_existing_key(test_client, monkeypatch):
    calls = install_litellm_stub(monkeypatch)
    first = mint(test_client)
    duplicate = test_client.post(
        "/v1/sessions",
        headers=worker_headers(),
        json={"run_id": "run-123", "operation": "coding"},
    )
    assert duplicate.status_code == 409
    assert "sk-one-time-secret" not in duplicate.text
    assert sum(call[1] == "/key/generate" for call in calls) == 1
    assert sum(call[1] == "/key/delete" for call in calls) == 1
    record = keybroker.get_session(first["session_id"])
    assert record is not None and record.revoked_at is not None


def test_expiration_reaper_revokes_and_marks_expired_sessions(test_client, monkeypatch):
    calls = install_litellm_stub(monkeypatch)
    session = mint(test_client)
    with keybroker.engine.begin() as connection:
        connection.execute(
            update(keybroker.SessionRecord)
            .where(keybroker.SessionRecord.session_id == session["session_id"])
            .values(expires_at=keybroker.utcnow() - timedelta(seconds=1))
        )

    assert asyncio.run(keybroker.reap_expired_sessions_once()) == 1
    record = keybroker.get_session(session["session_id"])
    assert record is not None and record.revoked_at is not None
    assert sum(call[1] == "/key/delete" for call in calls) == 1
    assert asyncio.run(keybroker.reap_expired_sessions_once()) == 0
    assert sum(call[1] == "/key/delete" for call in calls) == 1

def test_worker_authentication_uses_proxy_identity_and_not_body_worker_id(test_client, monkeypatch):
    install_litellm_stub(monkeypatch)
    assert test_client.post(
        "/v1/sessions", json={"run_id": "run-123", "operation": "coding"}
    ).status_code == 403
    assert test_client.post(
        "/v1/sessions",
        headers={**worker_headers(), "X-Darkfactory-Proxy-Token": "wrong"},
        json={"run_id": "run-123", "operation": "coding"},
    ).status_code == 403
    for address, worker_id in (
        ("100.70.246.104", "worker-06"),
        ("100.79.110.117", "worker-05"),
    ):
        admitted = test_client.post(
            "/v1/sessions",
            headers=worker_headers(address),
            json={"run_id": "run-123", "operation": "coding"},
        )
        assert admitted.status_code == 200
        record = keybroker.get_session(admitted.json()["session_id"])
        assert record is not None and record.worker_id == worker_id
    assert test_client.post(
        "/v1/sessions",
        headers=worker_headers(),
        json={"run_id": "run-123", "operation": "coding", "worker_id": "worker-03"},
    ).status_code == 422


def test_only_the_owning_worker_can_revoke_a_session(test_client, monkeypatch):
    calls = install_litellm_stub(monkeypatch)
    session = mint(test_client)
    denied = test_client.delete(
        f"/v1/sessions/{session['session_id']}",
        headers=worker_headers("100.88.92.78"),
    )
    assert denied.status_code == 404
    assert not any(call[1] == "/key/delete" for call in calls)

    revoked = test_client.delete(
        f"/v1/sessions/{session['session_id']}", headers=worker_headers()
    )
    assert revoked.json() == {"revoked": True, "session_id": session["session_id"]}
    record = keybroker.get_session(session["session_id"])
    assert record is not None and record.revoked_at is not None
    delete_call = next(call for call in calls if call[1] == "/key/delete")
    assert delete_call[2] == {"keys": ["token-id-123"]}


def test_exposure_revokes_matched_key_and_forwards_signed_safe_metadata(test_client, monkeypatch):
    calls = install_litellm_stub(monkeypatch)
    events: list[dict] = []
    ordering: list[str] = []
    original_request = keybroker.litellm_request

    async def ordered_request(method, path, *, params=None, body=None, allow_404=False):
        ordering.append("revoke" if path == "/key/delete" else path)
        return await original_request(method, path, params=params, body=body, allow_404=allow_404)

    async def capture_event(event):
        ordering.append("flag")
        events.append(event)
        return True

    monkeypatch.setattr(keybroker, "litellm_request", ordered_request)
    monkeypatch.setattr(keybroker, "forward_exposure", capture_event)
    session = mint(test_client)
    response = test_client.post(
        f"/v1/sessions/{session['session_id']}/exposure",
        headers=worker_headers(),
        json={
            "event_id": "event-789",
            "reason": "credential_pattern_detected",
            "session_key_match": True,
        },
    )
    assert response.status_code == 202
    assert response.json()["revoked"] is True
    assert response.json()["stop_run"] is True
    assert events == [
        {
            "event_id": "event-789",
            "key_id": "token-id-123",
            "key_alias": session["key_alias"],
            "source": "worker-omp",
            "reason": "credential_pattern_in_output",
            "occurred_at": events[0]["occurred_at"],
            "worker_id": "worker-02",
            "session_id": session["session_id"],
        }
    ]
    assert "key" not in events[0] and "prompt" not in events[0] and "output" not in events[0]

    raw, signature = keybroker.signed_event(events[0])
    expected = hmac.new(
        b"exposure-test-secret", raw, hashlib.sha256
    ).hexdigest()
    assert signature == f"sha256={expected}"
    assert json.loads(raw)["event_id"] == "event-789"
    assert response.json()["notification_pending"] is False
    with keybroker.Session(keybroker.engine) as db:
        outbox_event = db.get(keybroker.ExposureOutboxRecord, "event-789")
        assert outbox_event is not None and outbox_event.delivered_at is not None
    assert datetime.fromisoformat(events[0]["occurred_at"]).tzinfo == timezone.utc
    assert any(call[1] == "/key/delete" for call in calls)


def test_unassociated_pattern_stops_run_without_revoking_session(test_client, monkeypatch):
    calls = install_litellm_stub(monkeypatch)
    events: list[dict] = []

    async def capture_event(event):
        events.append(event)
        return True

    monkeypatch.setattr(keybroker, "forward_exposure", capture_event)
    session = mint(test_client)
    response = test_client.post(
        f"/v1/sessions/{session['session_id']}/exposure",
        headers=worker_headers(),
        json={
            "event_id": "event-999",
            "reason": "credential_pattern_detected",
            "session_key_match": False,
        },
    )
    assert response.status_code == 202
    assert response.json()["revoked"] is False
    assert response.json()["stop_run"] is True
    assert events[0]["key_id"] is None
    assert events[0]["key_alias"] == "unmapped"
    assert events[0]["session_id"] == session["session_id"]
    assert not any(call[1] == "/key/delete" for call in calls)



def test_matched_key_revocation_failure_still_flags_and_stops_run(test_client, monkeypatch):
    calls = install_litellm_stub(monkeypatch)
    events: list[dict] = []
    original_request = keybroker.litellm_request

    async def fail_revoke(method, path, *, params=None, body=None, allow_404=False):
        if path == "/key/delete":
            calls.append((method, path, body))
            raise keybroker.HTTPException(status_code=502, detail="LiteLLM unavailable")
        return await original_request(method, path, params=params, body=body)

    async def capture_event(event):
        events.append(event)
        return True

    session = mint(test_client)
    monkeypatch.setattr(keybroker, "litellm_request", fail_revoke)
    monkeypatch.setattr(keybroker, "forward_exposure", capture_event)
    response = test_client.post(
        f"/v1/sessions/{session['session_id']}/exposure",
        headers=worker_headers(),
        json={
            "event_id": "event-revoke-failure",
            "reason": "session_key_echo_detected",
            "session_key_match": True,
        },
    )
    assert response.status_code == 202
    assert response.json()["revoked"] is False
    assert response.json()["stop_run"] is True
    assert events[0]["key_id"] == "token-id-123"
    assert keybroker.get_session(session["session_id"]).revoked_at is None


def test_exposure_outbox_retries_after_api_guard_unavailable(test_client, monkeypatch):
    install_litellm_stub(monkeypatch)
    session = mint(test_client)
    calls = []

    async def unavailable(event):
        calls.append(event)
        return False

    monkeypatch.setattr(keybroker, "forward_exposure", unavailable)
    response = test_client.post(
        f"/v1/sessions/{session['session_id']}/exposure",
        headers=worker_headers(),
        json={
            "event_id": "event-queued",
            "reason": "session_key_echo_detected",
            "session_key_match": True,
        },
    )
    assert response.status_code == 202
    assert response.json()["notification_pending"] is True
    assert calls and "key" not in calls[0] and "prompt" not in calls[0]
    with keybroker.Session(keybroker.engine) as db:
        queued = db.get(keybroker.ExposureOutboxRecord, "event-queued")
        assert queued is not None and queued.delivered_at is None
        assert queued.attempts == 1
        assert "sk-one-time-secret" not in repr(queued.payload)

    with keybroker.engine.begin() as connection:
        connection.execute(
            update(keybroker.ExposureOutboxRecord)
            .where(keybroker.ExposureOutboxRecord.event_id == "event-queued")
            .values(next_attempt_at=keybroker.utcnow() - timedelta(seconds=1))
        )

    async def recovered(event):
        calls.append(event)
        return True

    monkeypatch.setattr(keybroker, "forward_exposure", recovered)
    assert asyncio.run(keybroker.deliver_pending_exposure_events_once()) == 1
    with keybroker.Session(keybroker.engine) as db:
        delivered = db.get(keybroker.ExposureOutboxRecord, "event-queued")
        assert delivered is not None and delivered.delivered_at is not None
        assert delivered.attempts == 2


def test_admin_session_history_is_safe_and_keeps_revocation_state(test_client, monkeypatch):
    install_litellm_stub(monkeypatch)
    created = mint(test_client)
    deleted = test_client.delete(
        f"/v1/sessions/{created['session_id']}", headers=worker_headers()
    )
    assert deleted.status_code == 200

    history = test_client.get(
        "/v1/admin/sessions?start=2026-10-01T00%3A00%3A00Z&end=2026-10-31T23%3A59%3A59Z",
        headers={"Authorization": "Bearer dashboard-test-token"},
    )
    assert history.status_code == 200
    result = history.json()
    assert result["total"] == 1
    row = result["data"][0]
    assert row["session_id"] == created["session_id"]
    assert row["worker_id"] == "worker-02"
    assert row["key_id"] == "token-id-123"
    assert row["alias"] == created["key_alias"]
    assert row["status"] == "revoked"
    assert "key" not in row
    assert "token_id" not in row
    assert "token_hash" not in row
    assert "run_id" not in row
    assert "sk-one-time-secret" not in history.text

def test_admin_routes_require_bearer_and_strip_raw_keys_and_content(test_client, monkeypatch):
    install_litellm_stub(monkeypatch)
    assert test_client.get("/v1/admin/keys").status_code == 403
    headers = {"Authorization": "Bearer dashboard-test-token"}
    keys = test_client.get("/v1/admin/keys", headers=headers)
    assert keys.status_code == 200
    assert keys.json()[0]["key_id"] == "token-id-123"
    assert "key" not in keys.json()[0]
    assert "sk-must-not-escape" not in keys.text

    assert test_client.get("/v1/admin/workers").status_code == 403
    roster = test_client.get("/v1/admin/workers", headers=headers)
    assert roster.status_code == 200
    assert roster.json()["total"] == 6
    sammy = next(row for row in roster.json()["data"] if row["host"] == "sammy")
    assert sammy["worker_id"] == "worker-05"
    assert sammy["tailnet_ip"] == "100.79.110.117"
    assert "sk-must-not-escape" not in roster.text

    logs = test_client.get(
        "/v1/admin/spend-logs?start=2026-10-01T00%3A00%3A00Z&end=2026-10-02T00%3A00%3A00Z&page=2&page_size=50",
        headers=headers,
    )
    assert logs.status_code == 200
    assert logs.json()["total_pages"] == 3
    row = logs.json()["data"][0]
    assert row["api_key"] == "token-id-123"
    assert row["model"] == "gpt-5.6-luna"
    assert "prompt" not in row and "response" not in row and "raw_key" not in row
    assert "private user content" not in logs.text
    assert "sk-must-not-escape" not in logs.text

    secret_model = "sk-or-v1-" + "a" * 32
    unsafe_rows, _, _ = keybroker.spend_rows(
        {"data": [{"model": secret_model}], "total_pages": 1}
    )
    assert "model" not in unsafe_rows[0]
    assert secret_model not in repr(unsafe_rows)


def test_admin_key_generation_enforces_model_and_budget_policy(test_client, monkeypatch):
    install_litellm_stub(monkeypatch)
    headers = {"Authorization": "Bearer dashboard-test-token"}
    base_body = {"alias": "admin-test", "models": ["luna"], "max_budget_usd": 20}
    disallowed = test_client.post(
        "/v1/admin/keys/generate",
        headers=headers,
        json={**base_body, "models": ["unconfigured-model"]},
    )
    assert disallowed.status_code == 422
    over_budget = test_client.post(
        "/v1/admin/keys/generate",
        headers=headers,
        json={**base_body, "max_budget_usd": 25001},
    )
    assert over_budget.status_code == 422
    generated = test_client.post(
        "/v1/admin/keys/generate", headers=headers, json=base_body
    )
    assert generated.status_code == 200
    assert generated.json() == {
        "key": "sk-one-time-secret",
        "key_id": "token-id-123",
        "alias": "admin-test",
    }
    assert generated.headers["cache-control"] == "no-store"


def test_admin_revoke_forwards_only_safe_key_identifier(test_client, monkeypatch):
    calls = install_litellm_stub(monkeypatch)
    response = test_client.post(
        "/v1/admin/keys/revoke",
        headers={"Authorization": "Bearer dashboard-test-token"},
        json={"key_id": "token-id-123"},
    )
    assert response.status_code == 200
    assert response.json() == {"revoked": True, "key_id": "token-id-123"}
    assert calls[-1] == ("POST", "/key/delete", {"keys": ["token-id-123"]})
