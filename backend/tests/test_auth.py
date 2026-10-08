"""Registration, password recovery and legacy-account migration."""
from __future__ import annotations

import hashlib
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

import app.auth as auth_module
from app.main import create_app


QUESTIONS = [
    {"question": "你第一次参加的运动是什么？", "answer": "篮球"},
    {"question": "你最喜欢的城市叫什么？", "answer": "杭州"},
    {"question": "你小时候的玩伴叫什么？", "answer": "小明"},
]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("INNOVATION_ADMIN_USER", "admin")
    monkeypatch.setenv("INNOVATION_ADMIN_PASSWORD", "admin-secret")
    monkeypatch.setenv("INNOVATION_SECRET_KEY", "test-only-secret-that-is-long-enough")
    app = create_app(tmp_path, demo=True)
    with TestClient(app) as client:
        yield client, tmp_path


def register(client, username="Alice", phone="13800138001", questions=QUESTIONS):
    return client.post("/api/auth/register", json={
        "username": username, "phone": phone, "password": "old-password-123",
        "security_questions": questions,
    })


def login(client, username="Alice", password="old-password-123"):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def headers(token):
    return {"Authorization": f"Bearer {token}"}


def test_registration_uniqueness_and_question_hashes(workspace):
    client, base = workspace
    response = register(client)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["username"] == "Alice"
    assert body["phone"] == "+8613800138001"
    assert body["email"] is None
    assert body["has_security_questions"] is True
    assert client.get("/api/auth/me", headers=headers(body["access_token"])).json()["username"] == "Alice"
    assert login(client, "alice").status_code == 200
    assert register(client, "ALICE", "13800138002").status_code == 409
    assert register(client, "Bob", "13800138001").status_code == 409
    assert register(client, "admin", "13800138003").status_code == 409
    assert register(client, "13800138004", "13800138004").status_code == 422
    with sqlite3.connect(base / "auth.sqlite3") as db:
        rows = db.execute("SELECT answer_salt, answer_hash FROM security_questions").fetchall()
        assert len(rows) == 3
        assert len({row[0] for row in rows}) == 3
        assert all(len(row[1]) == 64 for row in rows)
        raw = (base / "auth.sqlite3").read_bytes()
        assert "篮球".encode() not in raw
        assert "杭州".encode() not in raw
        assert "小明".encode() not in raw
    assert "/api/auth/verification-codes" not in client.get("/openapi.json").json()["paths"]


def test_registration_rejects_duplicate_questions_and_answers(workspace):
    client, _ = workspace
    duplicate_answer = [dict(item) for item in QUESTIONS]
    duplicate_answer[1]["answer"] = " 篮球 "
    assert register(client, questions=duplicate_answer).status_code == 422
    duplicate_question = [dict(item) for item in QUESTIONS]
    duplicate_question[1]["question"] = QUESTIONS[0]["question"]
    assert register(client, questions=duplicate_question).status_code == 422
    assert register(client, questions=QUESTIONS[:2]).status_code == 422


def test_two_distinct_answers_reset_password_and_revoke_sessions(workspace):
    client, _ = workspace
    initial = register(client).json()["access_token"]
    second = login(client).json()["access_token"]
    questions = client.get("/api/auth/security-questions", params={"username": "ALICE"}).json()["questions"]
    assert [q["index"] for q in questions] == [0, 1, 2]
    assert "answer" not in str(questions).lower()
    assert client.get("/api/auth/security-questions", params={"username": "missing"}).json() == {"questions": []}
    payload = {"username": "Alice", "new_password": "new-password-456",
               "answers": [{"index": 0, "answer": "篮球"}, {"index": 2, "answer": "小明"}]}
    assert client.post("/api/auth/password/reset", json=payload).status_code == 200
    assert client.get("/api/auth/me", headers=headers(initial)).status_code == 401
    assert client.get("/api/auth/me", headers=headers(second)).status_code == 401
    assert login(client).status_code == 401
    assert login(client, password="new-password-456").status_code == 200


def test_recovery_rejects_duplicate_slots_and_locks_guessing(workspace):
    client, base = workspace
    register(client)
    duplicate = {"username": "Alice", "new_password": "new-password-456",
                 "answers": [{"index": 0, "answer": "篮球"}, {"index": 0, "answer": "篮球"}]}
    assert client.post("/api/auth/password/reset", json=duplicate).status_code == 422
    one_right = {"username": "Alice", "new_password": "new-password-456",
                 "answers": [{"index": 0, "answer": "篮球"}, {"index": 1, "answer": "错误"}]}
    for index in range(5):
        response = client.post("/api/auth/password/reset", json=one_right)
        assert response.status_code == (423 if index == 4 else 400), response.text
    correct = {"username": "Alice", "new_password": "new-password-456",
               "answers": [{"index": 0, "answer": "篮球"}, {"index": 1, "answer": "杭州"}]}
    assert client.post("/api/auth/password/reset", json=correct).status_code == 423
    with sqlite3.connect(base / "auth.sqlite3") as db:
        db.execute("UPDATE recovery_locks SET locked_until=?", (int(time.time()) - 1,))
        db.commit()
    assert client.post("/api/auth/password/reset", json=correct).status_code == 200


def test_authenticated_change_also_requires_questions(workspace):
    client, _ = workspace
    token = register(client).json()["access_token"]
    payload = {"new_password": "changed-password", "answers": [
        {"index": 0, "answer": "篮球"}, {"index": 1, "answer": "错误"}]}
    assert client.post("/api/auth/password/change", json=payload, headers=headers(token)).status_code == 400
    payload["answers"][1]["answer"] = "杭州"
    assert client.post("/api/auth/password/change", json=payload, headers=headers(token)).status_code == 200
    assert client.get("/api/auth/me", headers=headers(token)).status_code == 401
    assert login(client, password="changed-password").status_code == 200


def test_password_login_lock_and_admin_logout(workspace):
    client, base = workspace
    register(client)
    for index in range(5):
        response = login(client, password="wrong-password")
        assert response.status_code == (423 if index == 4 else 401)
    assert login(client).status_code == 423
    assert client.post("/api/auth/password/reset", json={
        "username": "Alice", "new_password": "changed-password",
        "answers": [{"index": 0, "answer": "篮球"}, {"index": 1, "answer": "杭州"}],
    }).status_code == 423
    with sqlite3.connect(base / "auth.sqlite3") as db:
        db.execute("UPDATE users SET locked_until=?", (int(time.time()) - 1,))
        db.commit()
    assert login(client).status_code == 200
    admin = login(client, "admin", "admin-secret")
    assert admin.status_code == 200
    token = admin.json()["access_token"]
    assert client.post("/api/auth/logout", headers=headers(token)).status_code == 204
    assert client.get("/api/auth/me", headers=headers(token)).status_code == 401


def test_registration_ip_limit(workspace, monkeypatch):
    client, _ = workspace
    monkeypatch.setattr(auth_module, "REGISTRATIONS_PER_IP_HOUR", 2)
    assert register(client, "first", "13800138011").status_code == 201
    assert register(client, "second", "13800138012").status_code == 201
    assert register(client, "third", "13800138013").status_code == 429


def test_legacy_user_migration_retains_login_and_session(tmp_path, monkeypatch):
    monkeypatch.setenv("INNOVATION_ADMIN_USER", "admin")
    monkeypatch.setenv("INNOVATION_ADMIN_PASSWORD", "admin-secret")
    monkeypatch.setenv("INNOVATION_SECRET_KEY", "test-only-secret-that-is-long-enough")
    salt = b"0123456789abcdef"
    digest = auth_module.AuthService._password_hash("legacy-password", salt)
    legacy_token = "existing-session-token"
    dbpath = tmp_path / "auth.sqlite3"
    with sqlite3.connect(dbpath) as db:
        db.execute("""CREATE TABLE users (
            id TEXT PRIMARY KEY, phone TEXT NOT NULL UNIQUE, email TEXT NOT NULL UNIQUE,
            password_salt TEXT NOT NULL, password_hash TEXT NOT NULL,
            failed_attempts INTEGER NOT NULL DEFAULT 0, locked_until INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL)""")
        db.execute("INSERT INTO users VALUES (?, ?, ?, ?, ?, 0, 0, ?)",
                   ("user-legacy", "+8613800138099", "old@example.test", salt.hex(), digest, int(time.time())))
        db.execute("CREATE TABLE sessions(token_hash TEXT PRIMARY KEY, subject TEXT NOT NULL, expires_at INTEGER NOT NULL)")
        db.execute("INSERT INTO sessions VALUES (?, ?, ?)",
                   (hashlib.sha256(legacy_token.encode()).hexdigest(), "user-legacy", int(time.time()) + 3600))
        db.commit()
    with TestClient(create_app(tmp_path, demo=True)) as client:
        assert client.get("/api/auth/me", headers=headers(legacy_token)).json()["id"] == "user-legacy"
        account = login(client, "old@example.test", "legacy-password")
        assert account.status_code == 200, account.text
        assert account.json()["has_security_questions"] is False
        assert client.post("/api/auth/password/reset", json={
            "username": "old@example.test", "new_password": "changed-password",
            "answers": [{"index": 0, "answer": "x"}, {"index": 1, "answer": "y"}],
        }).status_code == 400
        enrollment = client.put("/api/auth/security-questions", json={"security_questions": QUESTIONS},
                                headers=headers(account.json()["access_token"]))
        assert enrollment.status_code == 200, enrollment.text
        assert client.put("/api/auth/security-questions", json={"security_questions": QUESTIONS},
                          headers=headers(account.json()["access_token"])).status_code == 409
        assert client.get("/api/auth/me", headers=headers(account.json()["access_token"])).json()[
            "has_security_questions"] is True
        assert register(client, "newuser", "13800138098").status_code == 201
