import json
from pathlib import Path

import httpx

from app.skill_evolution.hermes import InferenceRelay, sandbox_command


def test_relay_scopes_models_and_preserves_tool_calls():
    calls = []

    def request(role, body):
        calls.append((role, body))
        return {"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "call1", "type": "function", "function": {"name": "terminal", "arguments": '{"command":"pwd"}'}}]},
            "finish_reason": "tool_calls"}]}

    relay = InferenceRelay(request, {"exec": {"name": "fixture"}})
    try:
        with httpx.Client(trust_env=False) as client:
            base = relay.url("exec")
            assert client.get(base + "/models").json()["data"][0]["id"] == "fixture"
            assert client.get(base.replace(relay.token, "wrong") + "/models").status_code == 404
            assert client.post(relay.url("eval") + "/chat/completions", json={}).status_code == 404
            assert client.post(base + "/other", json={}).status_code == 404
            body = {"messages": [{"role": "user", "content": "test"}], "stream": True}
            result = client.post(base + "/chat/completions", json=body)
            chunks = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: {")]
            call = chunks[0]["choices"][0]["delta"]["tool_calls"][0]
            assert call["index"] == 0 and call["function"]["name"] == "terminal"
            assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"
            assert calls == [("exec", body)]
    finally:
        relay.close()


def test_relay_never_returns_provider_error_or_secret():
    def request(*args):
        raise RuntimeError("secret-provider-key")

    relay = InferenceRelay(request, {"exec": {"name": "fixture"}})
    try:
        response = httpx.post(relay.url("exec") + "/chat/completions", json={"messages": []}, trust_env=False)
        assert response.status_code == 502
        assert "secret-provider-key" not in response.text
    finally:
        relay.close()


def test_sandbox_only_mounts_runtime_and_one_case():
    args = sandbox_command(Path("/opt/icap-hermes"), Path("/var/lib/icap/skill-evolution/job/case1"),
                           Path("/var/lib/icap/skill-evolution/job/case1/workspace"), ["hermes", "chat"])
    mounts = [args[i + 1:i + 3] for i, arg in enumerate(args) if arg in {"--bind", "--ro-bind"}]
    assert [str(Path("/var/lib/icap/skill-evolution/job/case1"))] * 2 in mounts
    assert not any(source in {"/", "/var", "/var/lib/icap", "/etc", "/proc"} for source, target in mounts)
    assert "--clearenv" in args and "--unshare-pid" in args and "--die-with-parent" in args
