from __future__ import annotations

import httpx
import pytest

from api_extractor.http.client import Client, Request, Response, retry_after_seconds

URL = "https://demo.example.com/things"


@pytest.fixture
def client():
    with Client(retries=2) as made:
        yield made


@pytest.fixture(autouse=True)
def no_real_sleeping(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _seconds: None)


def test_tls_verifies_against_the_os_trust_store():
    """certifi ships public roots only; a corporate proxy re-signs with an internal one.

    Verification stays on — this changes which trust anchors are used, not whether the
    certificate is checked.
    """
    import ssl

    import truststore

    from api_extractor.http.client import default_ssl_context

    context = default_ssl_context()
    assert isinstance(context, truststore.SSLContext)
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_a_plain_get(httpx_mock, client):
    httpx_mock.add_response(url=URL, json={"data": [1, 2]})
    response = client.send(Request(method="GET", url=URL))
    assert response.status == 200
    assert response.body == {"data": [1, 2]}
    assert response.parsed is True
    assert response.elapsed_ms >= 0


def test_query_and_payload_and_headers_go_out(httpx_mock, client):
    httpx_mock.add_response(json={})
    client.send(
        Request(
            method="POST",
            url=URL,
            query={"pageSize": 100},
            payload={"assetType": "PUMP"},
            headers={"X-API-Key": "abc"},
        )
    )
    sent = httpx_mock.get_requests()[0]
    assert sent.url.params["pageSize"] == "100"
    assert b"PUMP" in sent.content
    assert sent.headers["X-API-Key"] == "abc"


def test_a_non_json_body_is_kept_verbatim(httpx_mock, client):
    httpx_mock.add_response(url=URL, status_code=500, text="<html>nope</html>", is_reusable=True)
    response = client.send(Request(method="GET", url=URL))
    assert response.parsed is False
    assert response.body is None
    assert response.text == "<html>nope</html>"


def test_a_4xx_is_returned_not_raised(httpx_mock, client):
    """A 403 is real information about the API, not an exception."""
    httpx_mock.add_response(url=URL, status_code=403, json={"error": "forbidden"})
    response = client.send(Request(method="GET", url=URL))
    assert response.status == 403
    assert response.body == {"error": "forbidden"}
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_other_4xx_are_never_retried(httpx_mock, client, status):
    httpx_mock.add_response(url=URL, status_code=status, json={})
    client.send(Request(method="GET", url=URL))
    assert len(httpx_mock.get_requests()) == 1


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_429_and_5xx_are_retried(httpx_mock, client, status):
    httpx_mock.add_response(url=URL, status_code=status, json={})
    httpx_mock.add_response(url=URL, status_code=status, json={})
    httpx_mock.add_response(url=URL, json={"ok": True})
    response = client.send(Request(method="GET", url=URL))
    assert response.status == 200
    assert len(httpx_mock.get_requests()) == 3


def test_retries_are_bounded_and_the_last_response_is_returned(httpx_mock, client):
    httpx_mock.add_response(url=URL, status_code=503, json={"still": "down"}, is_reusable=True)
    response = client.send(Request(method="GET", url=URL))
    assert response.status == 503
    assert response.body == {"still": "down"}
    assert len(httpx_mock.get_requests()) == 3  # 1 attempt + 2 retries


def test_retry_after_is_honoured(httpx_mock, client, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("time.sleep", slept.append)
    httpx_mock.add_response(url=URL, status_code=429, headers={"Retry-After": "7"}, json={})
    httpx_mock.add_response(url=URL, json={})
    client.send(Request(method="GET", url=URL))
    assert 7.0 in slept


def test_retry_after_parsing():
    def with_header(value: str) -> Response:
        return Response(status=429, headers={"Retry-After": value}, elapsed_ms=0, text="")

    assert retry_after_seconds(with_header("12")) == 12.0
    assert retry_after_seconds(with_header("garbage")) is None
    assert retry_after_seconds(Response(status=429, headers={}, elapsed_ms=0, text="")) is None
    # Capped, so a hostile header cannot park the run for a day.
    assert retry_after_seconds(with_header("99999")) == 300.0


def test_a_transport_error_propagates(httpx_mock, client):
    """The runner catches these, records them and carries on."""
    httpx_mock.add_exception(httpx.ConnectError("no route"))
    with pytest.raises(httpx.ConnectError):
        client.send(Request(method="GET", url=URL))


def test_rate_limit_sleeps_between_requests(httpx_mock, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("time.sleep", slept.append)
    httpx_mock.add_response(json={})
    httpx_mock.add_response(json={})
    with Client(retries=0, rate_limit=2) as client:  # 2/second -> 0.5s spacing
        client.send(Request(method="GET", url=URL))
        client.send(Request(method="GET", url=URL))
    assert slept and slept[0] <= 0.5


def test_no_rate_limit_means_no_sleeping(httpx_mock, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("time.sleep", slept.append)
    httpx_mock.add_response(json={})
    httpx_mock.add_response(json={})
    with Client(retries=0) as client:
        client.send(Request(method="GET", url=URL))
        client.send(Request(method="GET", url=URL))
    assert slept == []
