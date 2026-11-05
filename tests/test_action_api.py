import asyncio

import httpx
import pytest

import action_api


@pytest.fixture
def api(monkeypatch):
    """가짜 서버로 바꾼다. handler(request) -> httpx.Response를 정해 주면 된다."""
    calls = []

    def install(handler):
        def wrapped(request):
            calls.append(request)
            return handler(request)

        monkeypatch.setattr(action_api, "BASE_URL", "http://action.test")
        monkeypatch.setattr(action_api, "API_KEY", "k")
        monkeypatch.setattr(action_api, "RETRY_DELAY_SEC", 0)
        monkeypatch.setattr(action_api, "client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(wrapped)))
        return calls

    return install


def run(coro):
    return asyncio.run(coro)


def test_json_success(api):
    calls = api(lambda r: httpx.Response(200, json={"items": [1]}))
    result = run(action_api.call("GET", "/stock", params={"id": "A-001"}))
    assert result.ok and result.status == 200 and result.data == {"items": [1]}
    assert str(calls[0].url) == "http://action.test/stock?id=A-001"
    assert calls[0].headers["Authorization"] == "Bearer k"


def test_http_error_keeps_body(api):
    api(lambda r: httpx.Response(409, json={"message": "sold out"}))
    result = run(action_api.call("POST", "reserve", json={"qty": 3}))
    assert not result.ok and result.status == 409 and result.error == "http_409"
    assert result.data == {"message": "sold out"}


def test_retries_connect_error_once(api):
    def handler(request):
        if len(calls) == 1:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, text="ok")

    calls = api(handler)
    result = run(action_api.call("GET", "ping"))
    assert result.ok and result.data == "ok" and len(calls) == 2


def test_does_not_retry_timeout(api):
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    calls = api(handler)
    result = run(action_api.call("POST", "reserve", json={}))
    assert not result.ok and result.error == "timeout" and len(calls) == 1


def test_missing_base_url(monkeypatch):
    monkeypatch.setattr(action_api, "BASE_URL", "")
    result = run(action_api.call("GET", "ping"))
    assert not result.ok and "not set" in result.error
