"""MCP 서버 층: 도구 등록·설명 품질, 오류가 고칠 수 있는 JSON 으로 나가는지, 키 비노출."""

import asyncio
import json

import pytest

from varco_sound import server
from varco_sound.service import VarcoService

from .conftest import SECRET, make_settings, tone, write_wav

API_TOOLS = {"generate_sfx", "generate_from_sample", "make_loop", "mono_to_stereo", "creature_voice", "enhance_speech"}


def run(coro):
    return asyncio.run(coro)


def tools():
    return {t.name: t for t in run(server.mcp.list_tools())}


def test_all_tools_have_examples_and_cost():
    ts = tools()
    assert len(ts) == 17
    for name, t in ts.items():
        desc = t.description or ""
        assert "비용" in desc, name
        if name not in {"check_credits", "get_guide", "list_history", "estimate_cost", "build_audition_page"}:
            assert "예:" in desc, name
        if name in API_TOOLS:
            assert "크레딧" in desc and "언제 쓰지 말 것" in desc, name


def test_schema_exposes_ranges():
    props = tools()["generate_from_sample"].input_schema["properties"]
    assert props["strength"]["maximum"] == 3.0 and props["strength"]["minimum"] == 0.0


@pytest.fixture
def patched_service(tmp_path, client_factory, monkeypatch):
    svc = VarcoService(make_settings(tmp_path), client_factory())
    monkeypatch.setattr(server, "service", svc)
    monkeypatch.setattr(server, "settings", svc.settings)
    return svc


def call(name, args):
    """(오류 여부, 결과 또는 오류 메시지)"""
    return run(_call(name, args))


async def _call(name, args):
    from mcp.server.mcpserver.exceptions import ToolError
    try:
        return False, await server.mcp.call_tool(name, args)
    except ToolError as e:
        return True, str(e)


def test_validation_error_is_fixable_json(patched_service, tmp_path):
    src = write_wav(tmp_path / "long.wav", tone(11.0))
    is_err, msg = call("generate_from_sample", {"source": str(src)})
    assert is_err
    payload = json.loads(msg[msg.index("{"):])
    assert payload["error"] == "invalid_argument" and payload["allowed"] == "0.5~10.0s" and payload["fix"]


def test_api_error_json_has_no_key(patched_service, api):
    import httpx
    api.push(lambda r: httpx.Response(401, json={"message": f"Unauthorized {SECRET}"}))
    is_err, msg = call("generate_sfx", {"prompt": "kick", "n": 1})
    assert is_err and SECRET not in msg and "unauthorized" in msg


def test_unexpected_error_is_reported_without_key(patched_service, tmp_path):
    bad = tmp_path / "broken.wav"
    bad.write_bytes(b"RIFF....not really " + SECRET.encode())
    is_err, msg = call("analyze_audio", {"path": str(bad)})
    assert is_err and "internal_error" in msg and SECRET not in msg


def test_success_returns_structured(patched_service):
    is_err, result = call("generate_sfx", {"prompt": "kick", "n": 2})
    assert not is_err


def test_guide_tool_and_resource():
    is_err, _ = call("get_guide", {})
    assert not is_err
    assert "200자" in server.guide_resource()
