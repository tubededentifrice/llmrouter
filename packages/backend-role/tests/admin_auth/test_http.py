"""Focused administrator authentication HTTP tests."""
# ruff: noqa: D101, D102, D103, D107, EM101, PLR2004, PT018

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from llmrouter_backend.admin_auth.errors import AdministratorAuthError
from llmrouter_backend.admin_auth.http import router
from llmrouter_backend.admin_auth.model import SecretValue, SessionResult

NOW = datetime(2026, 8, 20, tzinfo=UTC)
SECRET = SecretValue("A" * 43)


def _callback_query(*, code: str = "code", state: str = SECRET.value) -> str:
    return urlencode(
        {
            "code": code,
            "state": state,
            "iss": "https://auth.opendle.dev",
            "scope": "openid offline_access",
        }
    )


class Repository:
    def __init__(self) -> None:
        self.trusted_grant_token: str | None = None
        self.session_token: str | None = None
        self.callback_session_token: SecretValue | None = SECRET
        self.reject_session_read = False
        self.logged_out = False

    def start_authorization(
        self, _purpose: object, _return_path: str, **kwargs: object
    ) -> SimpleNamespace:
        self.trusted_grant_token = cast("str | None", kwargs["trusted_grant_token"])
        self.session_token = cast("str | None", kwargs["session_token"])
        return SimpleNamespace(
            authorization_url="https://auth.opendle.dev/authorize?state=safe",
            expires_at=NOW + timedelta(minutes=5),
        )

    def complete_authorization(
        self, _code: str, _state: str, **_kwargs: object
    ) -> SessionResult:
        return _session(session_token=self.callback_session_token)

    def get_session(self, _token: str | None, **_kwargs: object) -> SessionResult:
        if not _token or self.reject_session_read:
            raise AdministratorAuthError("invalid_token", "request")
        return _session(session_token=None)

    def logout(
        self,
        _token: str,
        csrf: str,
        origin: str,
        **_kwargs: object,
    ) -> None:
        assert csrf == SECRET.value
        assert origin == "https://llmrouter.opendle.dev"
        self.logged_out = True


def _session(*, session_token: SecretValue | None) -> SessionResult:
    return SessionResult(
        session_token=session_token,
        csrf_token=SECRET,
        issuer="https://auth.opendle.dev",
        subject="person-one",
        grants=("grant-one",),
        authenticated_at=NOW,
        recent_authentication_at=NOW,
        account_state_checked_at=NOW,
        idle_expires_at=NOW + timedelta(days=7),
        absolute_expires_at=NOW + timedelta(days=7),
        return_path="/",
        identity_account_url="https://auth.opendle.dev/settings/account",
    )


def _client() -> tuple[TestClient, Repository]:
    app = FastAPI()
    app.include_router(router)
    repository = Repository()
    app.state.administrator_auth_repository = repository
    return TestClient(app), repository


def test_pocket_id_213_callback_shape_sets_the_session_cookie() -> None:
    client, repository = _client()
    started = client.post(
        "/v1/admin/session-starts",
        json={
            "purpose": "login",
            "return_path": "/",
            "trusted_grant_token": SECRET.value,
        },
    )
    assert started.status_code == 201
    assert repository.trusted_grant_token == SECRET.value
    callback = client.get(
        "/v1/admin/oidc/callback?" + _callback_query(),
        follow_redirects=False,
    )
    assert callback.status_code == 303
    cookie = callback.headers["set-cookie"]
    assert "__Host-llmrouter-admin=" in cookie
    assert "Max-Age=604800" in cookie
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie
    assert "Domain" not in cookie


def test_stale_cookie_is_cleared_and_does_not_block_new_login() -> None:
    client, repository = _client()
    repository.reject_session_read = True
    client.cookies.set("__Host-llmrouter-admin", SECRET.value)

    session = client.get("/v1/admin/session")
    started = client.post(
        "/v1/admin/session-starts",
        json={"purpose": "login", "return_path": "/"},
    )

    assert session.status_code == 401
    assert "Max-Age=0" in session.headers["set-cookie"]
    assert started.status_code == 201
    assert repository.session_token is None


def test_login_ignores_an_existing_session_cookie() -> None:
    client, repository = _client()
    client.cookies.set("__Host-llmrouter-admin", SECRET.value)

    started = client.post(
        "/v1/admin/session-starts",
        json={"purpose": "login", "return_path": "/"},
    )

    assert started.status_code == 201
    assert repository.session_token is None


def test_recent_authentication_keeps_the_current_session_cookie() -> None:
    client, repository = _client()
    client.cookies.set("__Host-llmrouter-admin", SECRET.value)
    repository.callback_session_token = None

    started = client.post(
        "/v1/admin/session-starts",
        json={"purpose": "recent_authentication", "return_path": "/"},
    )
    callback = client.get(
        "/v1/admin/oidc/callback?" + _callback_query(),
        follow_redirects=False,
    )

    assert started.status_code == 201
    assert repository.session_token == SECRET.value
    assert callback.status_code == 303
    assert callback.headers["location"] == "/"
    assert "set-cookie" not in callback.headers
    assert client.cookies.get("__Host-llmrouter-admin") == SECRET.value


def test_session_read_and_logout_are_no_store_and_browser_bound() -> None:
    client, repository = _client()
    client.cookies.set("__Host-llmrouter-admin", SECRET.value)
    session = client.get("/v1/admin/session")
    assert session.status_code == 200
    assert session.headers["cache-control"] == "no-store"
    assert session.json()["identity_account_url"].endswith("/settings/account")
    logout = client.delete(
        "/v1/admin/session",
        headers={
            "X-CSRF-Token": SECRET.value,
            "Origin": "https://llmrouter.opendle.dev",
        },
    )
    assert logout.status_code == 204
    assert repository.logged_out
    assert "Max-Age=0" in logout.headers["set-cookie"]


def test_missing_repository_is_a_bounded_temporary_failure() -> None:
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    started = client.post(
        "/v1/admin/session-starts",
        json={"purpose": "login", "return_path": "/"},
    )
    callback = client.get("/v1/admin/oidc/callback?" + _callback_query())
    assert started.status_code == 503
    assert callback.status_code == 503
    assert started.json()["error"]["code"] == "temporarily_unavailable"
    assert callback.json()["error"]["code"] == "temporarily_unavailable"


@pytest.mark.parametrize(
    ("body", "headers"),
    [
        (
            '{"purpose":"login","purpose":"recent_authentication","return_path":"/"}',
            {"Content-Type": "application/json"},
        ),
        (
            "x" * 4097,
            {"Content-Type": "application/json", "Content-Length": "4097"},
        ),
        ("{}", {"Content-Type": "text/plain"}),
    ],
)
def test_session_start_rejects_unbounded_or_ambiguous_http_input(
    body: str, headers: dict[str, str]
) -> None:
    client, _repository = _client()
    response = client.post("/v1/admin/session-starts", content=body, headers=headers)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize(
    "query",
    [
        _callback_query() + "&code=two",
        _callback_query(code="x" * 4097),
        _callback_query() + "&extra=1",
        _callback_query(state="short"),
        urlencode(
            {
                "code": "code",
                "state": SECRET.value,
                "iss": "https://attacker.example",
                "scope": "openid offline_access",
            }
        ),
        urlencode(
            {
                "code": "code",
                "state": SECRET.value,
                "iss": "https://auth.opendle.dev",
                "scope": "offline_access openid",
            }
        ),
        urlencode(
            {
                "code": "code",
                "state": SECRET.value,
                "iss": "https://auth.opendle.dev",
            }
        ),
        _callback_query() + "&iss=https%3A%2F%2Fauth.opendle.dev",
    ],
)
def test_callback_rejects_duplicate_unbounded_or_extra_query_values(
    query: str,
) -> None:
    client, _repository = _client()
    response = client.get(f"/v1/admin/oidc/callback?{query}")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert "set-cookie" not in response.headers


def test_public_host_does_not_accept_the_local_session_cookie() -> None:
    client, _repository = _client()
    app = cast("FastAPI", client.app)
    app.state.local_admin_authority = SimpleNamespace(
        valid_session=lambda token: token == SECRET.value,
        csrf=SECRET.value,
    )
    client.cookies.set("__Host-llmrouter-local-admin", SECRET.value)
    response = client.get(
        "/v1/admin/session", headers={"Host": "llmrouter.opendle.dev"}
    )
    assert response.status_code == 401
