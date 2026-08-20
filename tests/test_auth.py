from __future__ import annotations

import base64

import pytest

from api_extractor.auth import registered
from api_extractor.auth.registry import Authenticator
from api_extractor.config.models import Source
from api_extractor.http.client import Client
from api_extractor.logs import REDACTED, scrub
from tests.conftest import MINIMAL

BASE = "https://demo.example.com"


def auth_for(block: dict) -> Source:
    return Source.model_validate({**MINIMAL, "base_url": BASE, "auth": block})


@pytest.fixture
def client():
    with Client(retries=0) as made:
        yield made


def headers_for(block: dict, client) -> dict[str, str]:
    source = auth_for(block)
    return Authenticator(source.auth, client, source.base_url).headers()


def test_every_shipped_strategy_is_registered():
    assert registered() == [
        "basic",
        "bearer",
        "header",
        "login_token",
        "oauth_client_credentials",
    ]


def test_no_auth_means_no_headers(client):
    source = Source.model_validate(MINIMAL)
    assert Authenticator(source.auth, client, BASE).headers() == {}


def test_basic(monkeypatch, client):
    monkeypatch.setenv("U", "svc-account")
    monkeypatch.setenv("P", "hunter2-hunter2")
    headers = headers_for({"type": "basic", "username": "env:U", "password": "env:P"}, client)
    expected = base64.b64encode(b"svc-account:hunter2-hunter2").decode()
    assert headers == {"Authorization": f"Basic {expected}"}
    assert scrub(headers["Authorization"]) == f"Basic {REDACTED}"


def test_bearer_uses_the_default_apply(monkeypatch, client):
    monkeypatch.setenv("T", "tok-abcdef123")
    assert headers_for({"type": "bearer", "token": "env:T"}, client) == {
        "Authorization": "Bearer tok-abcdef123"
    }


def test_bearer_honours_a_custom_apply(monkeypatch, client):
    monkeypatch.setenv("T", "tok-abcdef123")
    headers = headers_for(
        {
            "type": "bearer",
            "token": "env:T",
            "apply": {"header": "X-Authorization", "template": "Token {token}"},
        },
        client,
    )
    assert headers == {"X-Authorization": "Token tok-abcdef123"}


def test_header_sends_a_bare_key_as_is(monkeypatch, client):
    monkeypatch.setenv("K", "key-abcdef123")
    assert headers_for({"type": "header", "headers": {"X-API-Key": "env:K"}}, client) == {
        "X-API-Key": "key-abcdef123"
    }


def test_header_sends_two_headers_one_templated(monkeypatch, client):
    monkeypatch.setenv("EDF", "edf-abcdef123")
    monkeypatch.setenv("ONE", "one-abcdef123")
    headers = headers_for(
        {
            "type": "header",
            "headers": {
                "X-Authorization": {"value": "env:EDF", "template": "ApiKey {value}"},
                "X-EDF-APIKey": "env:ONE",
            },
        },
        client,
    )
    assert headers == {
        "X-Authorization": "ApiKey edf-abcdef123",
        "X-EDF-APIKey": "one-abcdef123",
    }


LOGIN = {
    "type": "login_token",
    "request": {
        "method": "POST",
        "path": "/auth/login",
        "payload": {"username": "env:U", "password": "env:P"},
    },
    "token_path": "$.token",
    "apply": {"header": "X-Authorization", "template": "Bearer {token}"},
}


def test_login_token_acquires_and_applies(monkeypatch, httpx_mock, client):
    monkeypatch.setenv("U", "svc-account")
    monkeypatch.setenv("P", "hunter2-hunter2")
    httpx_mock.add_response(url=f"{BASE}/auth/login", json={"token": "issued-abc123"})
    assert headers_for(LOGIN, client) == {"X-Authorization": "Bearer issued-abc123"}


def test_login_token_sends_the_resolved_credentials(monkeypatch, httpx_mock, client):
    monkeypatch.setenv("U", "svc-account")
    monkeypatch.setenv("P", "hunter2-hunter2")
    httpx_mock.add_response(url=f"{BASE}/auth/login", json={"token": "issued-abc123"})
    headers_for(LOGIN, client)
    sent = httpx_mock.get_requests()[0]
    assert b"svc-account" in sent.content and b"env:U" not in sent.content


def test_an_acquired_token_is_scrubbed_from_logs(monkeypatch, httpx_mock, client):
    """It never passes through the environment, so nothing else would have caught it."""
    monkeypatch.setenv("U", "svc-account")
    monkeypatch.setenv("P", "hunter2-hunter2")
    httpx_mock.add_response(url=f"{BASE}/auth/login", json={"token": "issued-abc123"})
    headers_for(LOGIN, client)
    assert scrub("sending Bearer issued-abc123") == f"sending Bearer {REDACTED}"


def test_login_token_failure_is_loud(monkeypatch, httpx_mock, client):
    monkeypatch.setenv("U", "svc-account")
    monkeypatch.setenv("P", "hunter2-hunter2")
    httpx_mock.add_response(url=f"{BASE}/auth/login", status_code=403, json={})
    with pytest.raises(RuntimeError, match="returned 403"):
        headers_for(LOGIN, client)


def test_login_token_with_no_token_in_the_body(monkeypatch, httpx_mock, client):
    monkeypatch.setenv("U", "svc-account")
    monkeypatch.setenv("P", "hunter2-hunter2")
    httpx_mock.add_response(url=f"{BASE}/auth/login", json={"nope": 1})
    with pytest.raises(RuntimeError, match=r"no token at \$\.token"):
        headers_for(LOGIN, client)


def test_oauth_client_credentials(monkeypatch, httpx_mock, client):
    monkeypatch.setenv("CID", "client-abc123")
    monkeypatch.setenv("SEC", "secret-abc123")
    httpx_mock.add_response(
        url="https://demo.example.com/oauth/token", json={"access_token": "oauth-abc123"}
    )
    headers = headers_for(
        {
            "type": "oauth_client_credentials",
            "token_url": "https://demo.example.com/oauth/token",
            "client_id": "env:CID",
            "client_secret": "env:SEC",
            "scope": "read:things",
        },
        client,
    )
    assert headers == {"Authorization": "Bearer oauth-abc123"}
    assert b"read:things" in httpx_mock.get_requests()[0].content


def test_the_credential_is_acquired_once_per_run(monkeypatch, httpx_mock, client):
    monkeypatch.setenv("U", "svc-account")
    monkeypatch.setenv("P", "hunter2-hunter2")
    httpx_mock.add_response(url=f"{BASE}/auth/login", json={"token": "issued-abc123"})
    source = auth_for(LOGIN)
    authenticator = Authenticator(source.auth, client, BASE)
    for _ in range(5):
        authenticator.headers()
    assert len(httpx_mock.get_requests()) == 1


def test_refresh_acquires_again(monkeypatch, httpx_mock, client):
    monkeypatch.setenv("U", "svc-account")
    monkeypatch.setenv("P", "hunter2-hunter2")
    httpx_mock.add_response(url=f"{BASE}/auth/login", json={"token": "first-abc123"})
    httpx_mock.add_response(url=f"{BASE}/auth/login", json={"token": "second-abc123"})
    source = auth_for(LOGIN)
    authenticator = Authenticator(source.auth, client, BASE)
    assert authenticator.headers()["X-Authorization"] == "Bearer first-abc123"
    assert authenticator.refresh()["X-Authorization"] == "Bearer second-abc123"


def test_header_names_are_known_so_persist_can_redact_them(monkeypatch, client):
    monkeypatch.setenv("EDF", "edf-abcdef123")
    source = auth_for(
        {"type": "header", "headers": {"X-EDF-APIKey": "env:EDF", "X-Other": "env:EDF"}}
    )
    authenticator = Authenticator(source.auth, client, BASE)
    assert authenticator.header_names() == {"X-EDF-APIKey", "X-Other"}
