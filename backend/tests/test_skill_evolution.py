"""Exercise the real upstream engines in isolated workers against a local HTTP model."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("INNOVATION_ADMIN_USER", "admin")
    monkeypatch.setenv("INNOVATION_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("SKILL_EVOLVE_API_KEY", "")
    requests = []
    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            # Execution answer and a judge that explicitly awards zero. This must not
            # become a false 100-point pass in OPT's legacy rubric parser.
            is_judge = any("候选回答" in m["content"] for m in body["messages"])
            content = json.dumps({"score": 0, "reason": "incorrect", "suggestions": "fix output"}) if is_judge else "wrong answer"
            if body["model"] == "fixture-large":
                content = "large answer"
            elif any("候选回答：large answer" in m["content"] for m in body["messages"]):
                content = json.dumps({"score": 100, "reason": "correct"})
            if any("编辑专家" in m["content"] for m in body["messages"]):
                content = json.dumps([{"action": "REPLACE", "target": "Reply to the user.", "content": "Give a correct answer.", "bound_experience_id": "F1"}])
            elif any("失败经验分析专家" in m["content"] for m in body["messages"]):
                content = json.dumps([{"root_cause": "Wrong output", "suggestions": ["Give a correct answer"], "trajectory_ids": ["case1"], "trajectory_count": 1}])
            response = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(response)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    private = tmp_path / "skill-evolution.env"
    private.write_text(f"SKILL_EVOLVE_API_KEY=test-worker-private-secret\nSKILL_EVOLVE_API_BASE=http://127.0.0.1:{server.server_port}/v1\nSKILL_EVOLVE_MODEL=fixture-model", encoding="utf-8")
    app = create_app(tmp_path, demo=True)
    with TestClient(app) as client:
        token = client.post("/api/auth/login", json={"username": "admin", "password": "test-password"}).json()["access_token"]
        client.headers["Authorization"] = "Bearer " + token
        yield client, app, requests
    server.shutdown()
    server.server_close()


def payload(engine="opt", **changes):
    return {"name": "engine integration", "engine": engine, "operation": "evaluate", "skill_content": "Reply to the user.",
            "dataset": [{"id": "case1", "input": [{"type": "text", "data": "Question"}], "output": []}],
            **changes}


def wait(client, identifier):
    for _ in range(160):
        row = client.get(f"/api/skill-evolution/jobs/{identifier}").json()
        if row["status"] in {"completed", "failed", "cancelled"}:
            return row
        time.sleep(0.1)
    raise AssertionError("Worker did not finish")


@pytest.mark.parametrize("engine", ["opt", "enhanced"])
def test_real_upstream_worker_and_zero_score_contract(workspace, engine):
    client, app, requests = workspace
    response = client.post("/api/skill-evolution/jobs", json=payload(engine))
    assert response.status_code == 201, response.text
    row = wait(client, response.json()["id"])
    assert row["status"] == "completed", (row["error"], row["logs"])
    assert row["result"]["initial_score"] == 0, row
    assert row["result"]["best_score"] == 0
    assert row["result"]["rounds"][0]["cases"][0]["output"] == "wrong answer"
    assert requests[0]["messages"][0]["role"] == "system"
    assert requests[0]["messages"][1]["content"] == "Question"
    assert "test-worker-private-secret" not in json.dumps(row)
    stored = app.state.store.path.read_text(encoding="utf-8")
    assert "test-worker-private-secret" not in stored
    download = client.get(f"/api/skill-evolution/jobs/{row['id']}/artifacts/artifacts.zip")
    assert download.status_code == 200
    assert client.get(f"/api/skill-evolution/jobs/{row['id']}/artifacts/.env").status_code == 404


def test_input_validation_and_private_permissions(workspace):
    client, app, _ = workspace
    assert client.post("/api/skill-evolution/jobs", json=payload(resources=[{"name": "../escape.md", "content": "bad"}])).status_code == 422
    assert client.post("/api/skill-evolution/jobs", json=payload(dataset=[{"id": "../escape", "input": []}])).status_code == 422
    assert client.post("/api/skill-evolution/jobs", json=payload(dataset=[{"id": 1, "input": [{"type": "text", "data": "q"}], "output": [{"type": "onlyllm", "data": "/etc/passwd"}]}])).status_code == 422
    created = client.post("/api/skill-evolution/jobs", json=payload()).json()
    app.state.skill_service.update(created["id"], owner_user_id="another-user", visibility="private")
    other = {"id": "other", "role": "user"}
    with pytest.raises(Exception) as exc:
        from app.access import require_view
        require_view(app.state.skill_service.get(created["id"]), other)
    assert exc.value.status_code == 404
    with pytest.raises(Exception) as exc:
        app.state.skill_service.action(created["id"], "cancel", other)
    assert exc.value.status_code == 403


def test_call_budget_failure_is_not_success(workspace):
    client, _, requests = workspace
    response = client.post("/api/skill-evolution/jobs", json=payload("enhanced", max_model_calls=1))
    row = wait(client, response.json()["id"])
    assert row["status"] == "failed"
    assert len(requests) == 1
    assert "test-worker-private-secret" not in json.dumps(row)


@pytest.mark.parametrize("engine", ["opt", "enhanced"])
def test_structured_mutation_runs_second_round(workspace, engine):
    client, app, _ = workspace
    response = client.post("/api/skill-evolution/jobs", json=payload(engine, operation="evolve", rounds=2))
    row = wait(client, response.json()["id"])
    assert row["status"] == "completed", (row["error"], row["logs"])
    assert len(row["result"]["rounds"]) == 2, row["logs"]
    final = (app.state.skill_service.root / row["id"] / "final_skill.md").read_text(encoding="utf-8")
    assert "Give a correct answer." in final


def test_cancellation_of_queued_job(workspace):
    client, app, _ = workspace
    # Stop scheduling so cancellation deterministically hits a queued job.
    app.state.skill_service.stop.set()
    app.state.skill_service.thread.join(timeout=2)
    row = client.post("/api/skill-evolution/jobs", json=payload()).json()
    response = client.post(f"/api/skill-evolution/jobs/{row['id']}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert client.post(f"/api/skill-evolution/jobs/{row['id']}/resume").json()["status"] == "queued"


def test_enhanced_large_model_routes_only_after_failure(workspace):
    client, app, _ = workspace
    model = app.state.skill_service.default_model()
    response = client.post("/api/models", json={"id": "fixture-large", "name": "Fixture large", "provider": "Local",
                                              "endpoints": model["endpoints"]})
    assert response.status_code == 201, response.text
    response = client.post("/api/skill-evolution/jobs", json=payload("enhanced", large_model_id="fixture-large"))
    row = wait(client, response.json()["id"])
    assert row["status"] == "completed", (row["error"], row["logs"])
    case = row["result"]["rounds"][0]["cases"][0]
    assert case["route"] == "KNOWLEDGE", case
    assert case["large_output"] == "large answer"
    # Unverifiable model-generated evidence must not enter the knowledge pool.
    assert case["knowledge"].get("valid") is False


def test_enhanced_stall_triggers_bounded_exploration(workspace):
    client, _, _ = workspace
    response = client.post("/api/skill-evolution/jobs", json=payload("enhanced", operation="evolve", rounds=3,
                           stall_rounds=1, exploration_iterations=1))
    row = wait(client, response.json()["id"])
    assert row["status"] == "completed", (row["error"], row["logs"])
    attempts = [r["exploration"].get("attempts", []) for r in row["result"]["rounds"] if r["exploration"]]
    assert attempts and len(attempts[0]) == 1, row["logs"]
