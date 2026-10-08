"""End-to-end ownership checks across users and related resources."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("INNOVATION_ADMIN_USER", "admin")
    monkeypatch.setenv("INNOVATION_ADMIN_PASSWORD", "admin-secret")
    monkeypatch.setenv("INNOVATION_SECRET_KEY", "test-only-secret-that-is-long-enough")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    app = create_app(tmp_path, demo=True)
    with TestClient(app) as client:
        def account(index: int) -> dict:
            phone = f"1380013800{index}"
            response = client.post("/api/auth/register", json={
                "username": f"member{index}", "phone": phone, "password": "safe-password-123",
                "security_questions": [
                    {"question": f"你的第一份工作是什么 {index}？", "answer": f"first-{index}"},
                    {"question": f"你最喜欢的城市是哪里 {index}？", "answer": f"second-{index}"},
                    {"question": f"你的童年玩伴叫什么 {index}？", "answer": f"third-{index}"},
                ],
            })
            assert response.status_code == 201, response.text
            return response.json()

        users = [account(1), account(2)]
        response = client.post("/api/auth/login", json={
            "username": "admin", "password": "admin-secret",
        })
        assert response.status_code == 200, response.text
        yield client, users[0], users[1], response.json()


def headers(account: dict) -> dict:
    return {"Authorization": f"Bearer {account['access_token']}"}


def model_payload(model_id: str, visibility: str | None = None) -> dict:
    payload = {"id": model_id, "name": model_id, "provider": "Local", "endpoints": [{
        "url": "https://example.test/v1", "api_key": "test-only-key",
    }]}
    if visibility is not None:
        payload["visibility"] = visibility
    return payload


def test_model_visibility_and_owner_only_edit(workspace):
    client, owner, other, admin = workspace
    private = model_payload("private-model")
    response = client.post("/api/models", json=private, headers=headers(owner))
    assert response.status_code == 201, response.text
    assert response.json()["visibility"] == "private"
    assert response.json()["owner_user_id"] == owner["id"]
    assert response.json()["can_edit"] is True
    assert "api_key" not in response.json()["endpoints"][0]
    assert "private-model" not in {row["id"] for row in client.get("/api/models", headers=headers(other)).json()}
    assert "private-model" in {row["id"] for row in client.get("/api/models", headers=headers(admin)).json()}
    assert client.post("/api/school/infer", json={"model_id": "private-model", "prompt": "hi"},
                       headers=headers(other)).status_code == 404
    assert client.post("/api/agents/chat/stream", json={"model_id": "private-model", "message": "hi"},
                       headers=headers(other)).status_code == 404
    assert client.put("/api/models/private-model", json=private, headers=headers(other)).status_code == 403
    assert client.delete("/api/models/private-model", headers=headers(other)).status_code == 403

    public = model_payload("public-model", "public")
    response = client.post("/api/models", json=public, headers=headers(owner))
    assert response.status_code == 201, response.text
    visible = {row["id"]: row for row in client.get("/api/models", headers=headers(other)).json()}
    assert visible["public-model"]["can_edit"] is False
    assert client.post("/api/school/infer", json={"model_id": "public-model", "prompt": "hi"},
                       headers=headers(other)).status_code == 200
    assert client.put("/api/models/public-model", json=public, headers=headers(other)).status_code == 403
    assert client.delete("/api/models/public-model", headers=headers(other)).status_code == 403
    response = client.put("/api/models/public-model", json={**public, "name": "Admin edited"},
                          headers=headers(admin))
    assert response.status_code == 200 and response.json()["name"] == "Admin edited"
    assert response.json()["owner_user_id"] == owner["id"]
    assert client.delete("/api/models/private-model", headers=headers(admin)).status_code == 200


def test_dataset_files_follow_visibility_and_owner_rules(workspace):
    client, owner, other, admin = workspace
    response = client.post("/api/datasets", json={"name": "Private dataset"}, headers=headers(owner))
    assert response.status_code == 201, response.text
    private = response.json()
    assert private["visibility"] == "private" and private["owner_user_id"] == owner["id"]
    response = client.post(f"/api/datasets/{private['id']}/files", files={"file": ("secret.txt", b"owner data")},
                           headers=headers(owner))
    assert response.status_code == 201, response.text
    file_id = response.json()["id"]
    assert private["id"] not in {row["id"] for row in client.get("/api/datasets", headers=headers(other)).json()}
    assert private["id"] in {row["id"] for row in client.get("/api/datasets", headers=headers(admin)).json()}
    assert client.get(f"/api/datasets/{private['id']}/files/{file_id}",
                      headers=headers(other)).status_code == 404
    assert client.post(f"/api/datasets/{private['id']}/files", files={"file": ("x.txt", b"x")},
                       headers=headers(other)).status_code == 403
    assert client.delete(f"/api/datasets/{private['id']}", headers=headers(other)).status_code == 403
    assert client.get(f"/api/datasets/{private['id']}/files/{file_id}",
                      headers=headers(admin)).content == b"owner data"
    assert client.post(f"/api/datasets/{private['id']}/files", files={"file": ("admin.txt", b"admin data")},
                       headers=headers(admin)).status_code == 201

    response = client.post("/api/datasets", json={"name": "Public dataset", "visibility": "public"},
                           headers=headers(owner))
    public = response.json()
    response = client.post(f"/api/datasets/{public['id']}/files", files={"file": ("shared.txt", b"shared data")},
                           headers=headers(owner))
    assert response.status_code == 201
    shared_file = response.json()["id"]
    assert public["id"] in {row["id"] for row in client.get("/api/datasets", headers=headers(other)).json()}
    assert client.get(f"/api/datasets/{public['id']}/files/{shared_file}",
                      headers=headers(other)).content == b"shared data"
    assert client.post(f"/api/datasets/{public['id']}/files", files={"file": ("x.txt", b"x")},
                       headers=headers(other)).status_code == 403
    assert client.delete(f"/api/datasets/{public['id']}", headers=headers(other)).status_code == 403
    assert client.delete(f"/api/datasets/{public['id']}", headers=headers(admin)).status_code == 200


def test_public_evaluation_cannot_expose_private_dependencies(workspace):
    client, owner, other, admin = workspace
    for model_id, visibility in (("hidden-model", None), ("shared-model", "public")):
        response = client.post("/api/models", json=model_payload(model_id, visibility), headers=headers(owner))
        assert response.status_code == 201, response.text
    private_dataset = client.post("/api/datasets", json={"name": "Hidden data"},
                                  headers=headers(owner)).json()
    public_dataset = client.post("/api/datasets", json={"name": "Shared data", "visibility": "public"},
                                 headers=headers(owner)).json()
    cases = [
        ("hidden-model", public_dataset["id"], "public"),
        ("shared-model", private_dataset["id"], "public"),
        ("shared-model", public_dataset["id"], "private"),
        ("shared-model", public_dataset["id"], "public"),
    ]
    evaluation_ids = []
    for model_id, dataset_id, visibility in cases:
        response = client.post("/api/evaluations", json={
            "model_id": model_id, "dataset_id": dataset_id, "visibility": visibility,
        }, headers=headers(owner))
        assert response.status_code == 201, response.text
        evaluation_ids.append(response.json()["id"])
    visible_to_other = {row["id"] for row in client.get("/api/evaluations", headers=headers(other)).json()}
    assert visible_to_other == {evaluation_ids[3]}
    visible_to_admin = {row["id"] for row in client.get("/api/evaluations", headers=headers(admin)).json()}
    assert set(evaluation_ids) <= visible_to_admin
    assert client.patch(f"/api/evaluations/{evaluation_ids[0]}", json={"action": "cancel"},
                        headers=headers(admin)).status_code == 200
    for identifier in evaluation_ids[:3]:
        assert client.get(f"/api/evaluations/{identifier}", headers=headers(other)).status_code == 404
    assert client.patch(f"/api/evaluations/{evaluation_ids[3]}", json={"action": "cancel"},
                        headers=headers(other)).status_code == 403
    assert client.post("/api/evaluations", json={"model_id": "hidden-model",
                                                  "dataset_id": public_dataset["id"]},
                       headers=headers(other)).status_code == 404
    assert client.post("/api/evaluations", json={"model_id": "shared-model",
                                                  "dataset_id": private_dataset["id"]},
                       headers=headers(other)).status_code == 404
