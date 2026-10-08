import json
import time

from fastapi.testclient import TestClient

from app.main import create_app


def endpoint(url="http://127.0.0.1:9001/v1", api_key="local-secret"):
    return {"url": url, "api_key": api_key, "thinking_schema": {
        "enabled": {"thinking": {"type": "enabled"}},
        "disabled": {"thinking": {"type": "disabled"}},
    }, "self_deployed": True, "max_context_length": 32768,
        "top_k": 40, "top_p": 1, "temperature": 1}


def client_with_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("INNOVATION_ADMIN_USER", "admin")
    monkeypatch.setenv("INNOVATION_ADMIN_PASSWORD", "demo1234")
    client = TestClient(create_app(tmp_path, demo=True))
    response = client.post("/api/auth/login", json={"username": "admin", "password": "demo1234"})
    assert response.status_code == 200
    client.headers.update({"Authorization": f"Bearer {response.json()['access_token']}"})
    for model_id in ("fixture-a", "fixture-b"):
        response = client.post("/api/models", json={"id": model_id, "name": model_id,
            "provider": "本地", "endpoints": [endpoint()]})
        assert response.status_code == 201
    return client


def test_login_rejects_unicode_credentials_without_server_error(tmp_path, monkeypatch):
    monkeypatch.setenv("INNOVATION_ADMIN_USER", "admin")
    monkeypatch.setenv("INNOVATION_ADMIN_PASSWORD", "secret")
    with TestClient(create_app(tmp_path, demo=True)) as client:
        for username, password in (("错误用户名", "secret"), ("admin", "错误密码")):
            response = client.post("/api/auth/login", json={"username": username, "password": password})
            assert response.status_code == 401


def test_model_dataset_file_and_auth(tmp_path, monkeypatch):
    with client_with_auth(tmp_path, monkeypatch) as client:
        assert client.get("/api/models").status_code == 200
        model = client.post("/api/models", json={"id": "custom-model", "name": "测试模型",
            "provider": "内部", "endpoints": [endpoint()]}).json()
        assert model["name"] == "测试模型"
        assert "api_key" not in model["endpoints"][0]
        assert model["endpoints"][0]["has_api_key"] is True
        assert client.post("/api/models", json={"id": "custom-model", "name": "重复",
            "provider": "内部", "endpoints": [endpoint()]}).status_code == 409
        assert client.put("/api/models/custom-model", json={"id": "changed-id", "name": "测试模型",
            "provider": "内部", "endpoints": [endpoint()]}).status_code == 422
        dataset = client.post("/api/datasets", json={"name": "验证数据"}).json()
        upload = client.post(f"/api/datasets/{dataset['id']}/files", files={"file": ("cases.txt", b"case 1\n")})
        assert upload.status_code == 201
        file_id = upload.json()["id"]
        assert client.get(f"/api/datasets/{dataset['id']}/files/{file_id}").content == b"case 1\n"
        annotation = client.post("/api/annotations", json={"dataset_id": dataset["id"], "content": "case 1", "label": "通过"})
        assert annotation.status_code == 201
        assert client.get("/api/data-governance").json()["downloads"] == 1
        assert client.get("/api/data-governance").json()["annotations"] == 1
        assert client.get("/api/models", headers={"Authorization": "Bearer invalid"}).status_code == 401


def test_evaluation_lifecycle_and_report(tmp_path, monkeypatch):
    with client_with_auth(tmp_path, monkeypatch) as client:
        model_id = client.get("/api/models").json()[0]["id"]
        second_model = client.get("/api/models").json()[1]["id"]
        dataset_id = client.get("/api/datasets").json()[0]["id"]
        batch = client.post("/api/evaluations/batch", json={"model_ids": [model_id, second_model], "dataset_id": dataset_id, "benchmark": "ASR"})
        assert batch.status_code == 201 and len(batch.json()) == 2
        task = client.post("/api/evaluations", json={"model_id": model_id, "dataset_id": dataset_id, "benchmark": "Ruler1"}).json()
        assert task["status"] == "queued"
        pause = client.patch(f"/api/evaluations/{task['id']}", json={"action": "pause"})
        assert pause.json()["status"] == "paused"
        assert client.patch(f"/api/evaluations/{task['id']}", json={"action": "resume"}).json()["status"] == "running"
        deadline = time.monotonic() + 22
        while time.monotonic() < deadline:
            status = client.get(f"/api/evaluations/{task['id']}").json()
            if status["status"] == "completed":
                break
            time.sleep(0.3)
        assert status["status"] == "completed"
        with client.stream("GET", f"/api/evaluations/{task['id']}/events") as response:
            payload = "".join(response.iter_text())
            assert response.status_code == 200
            assert "event: state" in payload
        report = next(r for r in client.get("/api/reports").json() if r["evaluation_id"] == task["id"])
        assert report["simulated"] is True
        assert client.patch(f"/api/reports/{report['id']}/publish").json()["published"] is True


def test_agent_school_and_monitor(tmp_path, monkeypatch):
    with client_with_auth(tmp_path, monkeypatch) as client:
        tool = client.post("/api/agents/tools/prompt", json={"content": "给我一个提示词"}).json()
        assert tool["simulated"] is True
        job = client.post("/api/school/jobs", json={"name": "demo", "script": "print('safe')", "accelerator": "NPU"}).json()
        assert job["status"] == "queued"
        assert "不会执行" in job["logs"][0]
        token = client.headers["Authorization"].split()[1]
        with client.websocket_connect("/ws/monitor") as ws:
            ws.send_json({"token": token})
            metrics = ws.receive_json()
            assert metrics["simulated"] is True and len(metrics["devices"]) == 4
        with client.stream("POST", "/api/agents/chat/stream", json={"model_id": "fixture-a", "message": "你好"}) as response:
            assert "event: error" in "".join(response.iter_text())


def test_model_endpoints_round_robin_and_real_stream_contract(tmp_path, monkeypatch):
    with client_with_auth(tmp_path, monkeypatch) as client:
        original = next(row for row in client.get("/api/models").json() if row["id"] == "fixture-a")
        first = {**endpoint(api_key=""), "endpoint_id": original["endpoints"][0]["endpoint_id"]}
        second = endpoint("http://127.0.0.1:9002/v1", "second-secret")
        first["top_p"], second["top_p"] = 0.3, 0.7
        updated = client.put("/api/models/fixture-a", json={"id": "fixture-a", "name": "可对战模型",
            "provider": "本地", "endpoints": [first, second]})
        assert updated.status_code == 200
        assert len(updated.json()["endpoints"]) == 2
        assert "local-secret" not in json.dumps(client.get("/api/models").json())
        assert "second-secret" not in json.dumps(client.get("/api/models").json())

        calls = []

        class FakeUpstream:
            status_code = 200
            headers = {"content-type": "text/event-stream"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def aiter_lines(self):
                yield 'data: {"choices":[{"delta":{"reasoning_content":"思考"}}]}'
                yield 'data: {"choices":[{"delta":{"content":"回答"}}]}'
                yield 'data: [DONE]'

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            def stream(self, method, url, *, headers, json):
                calls.append((method, url, headers, json))
                return FakeUpstream()

        monkeypatch.setattr("app.main.httpx.AsyncClient", FakeClient)
        for thinking in (True, False):
            reply = client.post("/api/agents/chat/stream", json={"model_id": "fixture-a",
                "message": "你好", "thinking": thinking, "top_p": 0.8, "top_k": 25, "temperature": 0.4})
            assert reply.status_code == 200
            assert "event: reasoning" in reply.text and "event: chunk" in reply.text
            assert "event: done" in reply.text

        assert [call[1] for call in calls] == [
            "http://127.0.0.1:9001/v1/chat/completions",
            "http://127.0.0.1:9002/v1/chat/completions",
        ]
        assert [call[2]["Authorization"] for call in calls] == ["Bearer local-secret", "Bearer second-secret"]
        assert [call[3]["thinking"]["type"] for call in calls] == ["enabled", "disabled"]
        assert all(call[3]["top_k"] == 25 and call[3]["top_p"] == 0.8 for call in calls)

        for _ in range(2):
            assert client.post("/api/agents/chat/stream", json={"model_id": "fixture-a", "message": "默认参数"}).status_code == 200
        assert [call[3]["top_p"] for call in calls[2:]] == [0.3, 0.7]


def test_deepseek_model_edit_keeps_key_server_side(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "environment-secret")
    monkeypatch.setenv("INNOVATION_ADMIN_USER", "admin")
    monkeypatch.setenv("INNOVATION_ADMIN_PASSWORD", "demo1234")

    class CatalogResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"id": "deepseek-flash", "name": "DeepSeek Flash",
                "context_window": 131072, "input_modalities": ["text"]}]}

    monkeypatch.setattr("app.model_registry.httpx.get", lambda *args, **kwargs: CatalogResponse())
    app = create_app(tmp_path, demo=True)
    with TestClient(app) as client:
        token = client.post("/api/auth/login", json={"username": "admin", "password": "demo1234"}).json()["access_token"]
        client.headers.update({"Authorization": f"Bearer {token}"})
        model = client.get("/api/models").json()[0]
        assert model["endpoints"][0]["has_api_key"] is True
        assert "environment-secret" not in json.dumps(model)

        initial = endpoint("https://api.deepseek.com", "")
        initial["endpoint_id"] = "deepseek-default"
        edited = client.put("/api/models/deepseek-flash", json={"id": "deepseek-flash",
            "name": "My Flash", "provider": "DeepSeek", "endpoints": [initial]})
        assert edited.status_code == 200
        stored = app.state.store.snapshot()["models"][0]["endpoints"][0]
        assert stored["key_env"] == "DEEPSEEK_API_KEY" and stored["api_key"] == ""

        changed = {**initial, "url": "http://127.0.0.1:9003/v1"}
        edited = client.put("/api/models/deepseek-flash", json={"id": "deepseek-flash",
            "name": "My Flash", "provider": "DeepSeek", "endpoints": [changed]})
        assert edited.status_code == 200
        assert edited.json()["endpoints"][0]["has_api_key"] is False
        assert "key_env" not in app.state.store.snapshot()["models"][0]["endpoints"][0]


def test_recommendations_never_return_model_api_keys(tmp_path, monkeypatch):
    with client_with_auth(tmp_path, monkeypatch) as client:
        def add_scores(data):
            data["models"][0]["scores"] = [80, 75, 90, 70, 85]

        client.app.state.store.update(add_scores)
        response = client.get("/api/recommendations?scenario=language")
        assert response.status_code == 200
        assert response.json()["rows"]
        assert "local-secret" not in response.text
