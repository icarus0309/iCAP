import json
import time

from fastapi.testclient import TestClient

from app.main import create_app


def client_with_auth(tmp_path):
    client = TestClient(create_app(tmp_path, demo=True))
    response = client.post("/api/auth/login", json={"username": "admin", "password": "demo1234"})
    assert response.status_code == 200
    client.headers.update({"Authorization": f"Bearer {response.json()['access_token']}"})
    return client


def test_model_dataset_file_and_auth(tmp_path):
    with client_with_auth(tmp_path) as client:
        assert client.get("/api/models").status_code == 200
        model = client.post("/api/models", json={"name": "测试模型", "provider": "内部", "scores": [70, 80, 90, 60, 75]}).json()
        assert model["name"] == "测试模型"
        assert any(row["id"] == model["id"] for row in client.get("/api/leaderboard").json()["rows"])
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


def test_evaluation_lifecycle_and_report(tmp_path):
    with client_with_auth(tmp_path) as client:
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


def test_agent_school_and_monitor(tmp_path):
    with client_with_auth(tmp_path) as client:
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
        with client.stream("POST", "/api/agents/chat/stream", json={"message": "你好"}) as response:
            assert "event: chunk" in "".join(response.iter_text())
