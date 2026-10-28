"""NcAiClient(공통): 재시도 정책, HTTP 오류 변환, 키 비노출."""

import asyncio
import json
import logging

import httpx
import pytest

from nc_ai_common.config import ApiSettings
from nc_ai_common.errors import NcAiError
from nc_ai_common.http_client import NcAiClient

SECRET = "SECRET-TEST-KEY-1234567890"


class Fake:
    def __init__(self):
        self.requests, self.queue = [], []

    def handler(self, request):
        self.requests.append(request)
        return self.queue.pop(0)(request) if self.queue else httpx.Response(200, json={"ok": True})


@pytest.fixture
def fake():
    return Fake()


def client(fake, key=SECRET, retries=2):
    c = NcAiClient(ApiSettings(key, "https://api.test", 5, retries), transport=httpx.MockTransport(fake.handler))

    async def no_sleep(_):
        return None
    c._sleep = no_sleep
    return c


def post(c):
    return asyncio.run(c.post_json("/x", {"a": 1}))


def test_success_returns_body_and_meta(fake):
    fake.queue.append(lambda r: httpx.Response(200, json={"v": 1}, headers={"x-request-id": "rid-1"}))
    res = post(client(fake))
    assert res.body == {"v": 1} and res.attempts == 1 and res.request_id == "rid-1"


def test_key_in_header_only(fake):
    post(client(fake))
    req = fake.requests[0]
    assert req.headers["OPENAPI_KEY"] == SECRET
    assert SECRET not in str(req.url) and SECRET.encode() not in req.content
    assert json.loads(req.content) == {"a": 1}


def test_422_not_retried_and_explains_field(fake):
    fake.queue.append(lambda r: httpx.Response(422, json={"detail": [{"loc": ["body", "num_sample"], "msg": "must be <= 3"}]}))
    with pytest.raises(NcAiError) as e:
        post(client(fake))
    assert e.value.code == "api_rejected_input" and "num_sample" in e.value.message
    assert e.value.fix and len(fake.requests) == 1


@pytest.mark.parametrize("status,code", [(401, "unauthorized"), (402, "insufficient_credits"), (400, "api_rejected_input"),
                                         (404, "http_error")])
def test_client_errors_not_retried(fake, status, code):
    fake.queue.append(lambda r: httpx.Response(status, json={"message": "nope"}))
    with pytest.raises(NcAiError) as e:
        post(client(fake))
    assert e.value.code == code and not e.value.retryable and len(fake.requests) == 1


def test_5xx_retried_then_succeeds(fake):
    fake.queue += [lambda r: httpx.Response(500, json={"message": "boom"}), lambda r: httpx.Response(503, text="x")]
    assert post(client(fake)).attempts == 3


def test_429_retried(fake):
    fake.queue.append(lambda r: httpx.Response(429, headers={"retry-after": "1"}, json={"message": "slow"}))
    assert post(client(fake)).attempts == 2


def test_retries_exhausted_reports_attempts(fake):
    fake.queue += [lambda r: httpx.Response(500, json={"message": "boom"})] * 3
    with pytest.raises(NcAiError) as e:
        post(client(fake, retries=2))
    d = e.value.to_dict()
    assert d["error"] == "server_error" and d["attempts"] == 3 and "_retry_after" not in d


def test_network_error_retried_then_raised(fake):
    def boom(r):
        raise httpx.ConnectError("refused", request=r)
    fake.queue += [boom] * 3
    with pytest.raises(NcAiError) as e:
        post(client(fake, retries=2))
    assert e.value.code == "network_error" and len(fake.requests) == 3


def test_non_json_success(fake):
    fake.queue.append(lambda r: httpx.Response(200, text="<html>"))
    with pytest.raises(NcAiError) as e:
        post(client(fake))
    assert e.value.code == "bad_response"


def test_missing_key_fails_before_request(fake):
    with pytest.raises(NcAiError) as e:
        post(client(fake, key=None))
    assert e.value.code == "missing_api_key" and not fake.requests


def test_key_never_leaks_into_errors_or_logs(fake, caplog):
    """서버가 오류 본문에 키를 되돌려주거나 예외 메시지에 키가 섞여도 지워져야 한다."""
    caplog.set_level(logging.DEBUG)
    fake.queue += [lambda r: httpx.Response(500, json={"message": f"bad {SECRET}"}),
                   lambda r: httpx.Response(401, json={"message": f"Unauthorized {SECRET}"})]
    with pytest.raises(NcAiError) as e:
        post(client(fake, retries=1))
    assert SECRET not in e.value.to_json()

    def leak(r):
        raise httpx.ConnectError(f"proxy said {SECRET}", request=r)
    fake.queue += [leak] * 2
    with pytest.raises(NcAiError) as e2:
        post(client(fake, retries=1))
    assert SECRET not in e2.value.to_json() and SECRET not in caplog.text
