"""SQLite accounts, security questions and revocable sessions."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from fastapi import HTTPException, Request

LOGIN_PER_IDENTIFIER_HOUR = 20
LOGIN_PER_IP_HOUR = 40
REGISTRATIONS_PER_IP_HOUR = 10
RECOVERY_PER_IDENTIFIER_HOUR = 20
RECOVERY_PER_IP_HOUR = 40
MAX_FAILURES = 5
LOCK_SECONDS = 600
SESSION_LIFETIME = 28800
PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff]{3,32}$")


def normalize_phone(raw: str) -> str:
    value = raw.strip().replace(" ", "").replace("-", "")
    if value.startswith("+86"):
        value = value[3:]
    if not PHONE_RE.fullmatch(value):
        raise HTTPException(422, "请输入有效的中国大陆手机号")
    return f"+86{value}"


def normalize_username(raw: str) -> tuple[str, str]:
    username = unicodedata.normalize("NFKC", raw.strip())
    if not USERNAME_RE.fullmatch(username) or PHONE_RE.fullmatch(username):
        raise HTTPException(422, "用户名须为 3–32 位中文、字母、数字或下划线，且不能是手机号")
    return username, username.casefold()


def normalize_answer(raw: str) -> str:
    return unicodedata.normalize("NFKC", raw.strip()).casefold()


def source_ip(request: Request) -> str:
    """Trust Cloudflare client IP only behind a loopback tunnel."""
    peer = request.client.host if request.client else "unknown"
    try:
        loopback = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        loopback = False
    if loopback:
        forwarded = request.headers.get("CF-Connecting-IP", "")
        try:
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
    return peer


class AuthService:
    def __init__(self, base: Path, secret: str, admin_user: str, admin_password: str, demo: bool):
        self.path = base / "auth.sqlite3"
        self.secret = secret.encode("utf-8")
        self.admin_user = admin_user
        self.admin_password = admin_password
        self.demo = demo
        self._dummy_salt = secrets.token_bytes(16)
        self._hash_slots = threading.BoundedSemaphore(2)
        self._initialize()

    def _connection(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout = 10000")
        db.execute("PRAGMA foreign_keys = ON")
        return db

    @contextmanager
    def _transaction(self):
        db = self._connection()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _create_users(db: sqlite3.Connection) -> None:
        db.execute("""CREATE TABLE users (
            id TEXT PRIMARY KEY, username TEXT NOT NULL, username_key TEXT NOT NULL UNIQUE,
            phone TEXT NOT NULL UNIQUE, email TEXT UNIQUE,
            password_salt TEXT NOT NULL, password_hash TEXT NOT NULL,
            has_security_questions INTEGER NOT NULL DEFAULT 0,
            failed_attempts INTEGER NOT NULL DEFAULT 0, locked_until INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL)""")

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            columns = {r["name"] for r in db.execute("PRAGMA table_info(users)")}
            if not columns:
                self._create_users(db)
            elif "username_key" not in columns:
                # Preserve user IDs, old password hashes, email/phone and session subjects.
                db.execute("ALTER TABLE users RENAME TO users_legacy")
                self._create_users(db)
                db.execute("""INSERT INTO users
                    (id, username, username_key, phone, email, password_salt, password_hash,
                     has_security_questions, failed_attempts, locked_until, created_at)
                    SELECT id, email, lower(email), phone, email, password_salt, password_hash,
                           0, failed_attempts, locked_until, created_at FROM users_legacy""")
                db.execute("DROP TABLE users_legacy")
            db.execute("""CREATE TABLE IF NOT EXISTS security_questions (
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                position INTEGER NOT NULL CHECK(position BETWEEN 0 AND 2),
                question TEXT NOT NULL, answer_salt TEXT NOT NULL, answer_hash TEXT NOT NULL,
                PRIMARY KEY(user_id, position))""")
            db.execute("""CREATE TABLE IF NOT EXISTS admin_locks (
                username TEXT PRIMARY KEY, failed_attempts INTEGER NOT NULL DEFAULT 0,
                locked_until INTEGER NOT NULL DEFAULT 0)""")
            db.execute("""CREATE TABLE IF NOT EXISTS login_requests (
                target_hash TEXT NOT NULL, source_ip TEXT NOT NULL, requested_at INTEGER NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS login_requests_target_time ON login_requests(target_hash, requested_at)")
            db.execute("CREATE INDEX IF NOT EXISTS login_requests_ip_time ON login_requests(source_ip, requested_at)")
            db.execute("""CREATE TABLE IF NOT EXISTS registration_requests (
                source_ip TEXT NOT NULL, requested_at INTEGER NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS registration_requests_ip_time ON registration_requests(source_ip, requested_at)")
            db.execute("""CREATE TABLE IF NOT EXISTS recovery_requests (
                target_hash TEXT NOT NULL, source_ip TEXT NOT NULL, requested_at INTEGER NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS recovery_requests_target_time ON recovery_requests(target_hash, requested_at)")
            db.execute("CREATE INDEX IF NOT EXISTS recovery_requests_ip_time ON recovery_requests(source_ip, requested_at)")
            db.execute("""CREATE TABLE IF NOT EXISTS recovery_locks (
                target_hash TEXT PRIMARY KEY, failed_attempts INTEGER NOT NULL DEFAULT 0,
                locked_until INTEGER NOT NULL DEFAULT 0)""")
            db.execute("""CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY, subject TEXT NOT NULL, expires_at INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS revoked_legacy_tokens (
                token_hash TEXT PRIMARY KEY, expires_at INTEGER NOT NULL)""")
            db.execute("DROP TABLE IF EXISTS verification_codes")
            db.execute("DROP TABLE IF EXISTS code_requests")

    def _guarded_hash(self, material: bytes, salt: bytes) -> str:
        if not self._hash_slots.acquire(blocking=False):
            raise HTTPException(429, "请求过于频繁，请稍后重试", headers={"Retry-After": "1"})
        try:
            return hashlib.scrypt(material, salt=salt, n=131072, r=8, p=1,
                                  dklen=32, maxmem=256 * 1024 * 1024).hex()
        finally:
            self._hash_slots.release()

    @staticmethod
    def _password_hash(password: str, salt: bytes) -> str:
        return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=131072, r=8, p=1,
                              dklen=32, maxmem=256 * 1024 * 1024).hex()

    def _guarded_password_hash(self, password: str, salt: bytes) -> str:
        return self._guarded_hash(password.encode("utf-8"), salt)

    def _answer_hash(self, answer: str, salt: bytes) -> str:
        peppered = hmac.new(self.secret, normalize_answer(answer).encode("utf-8"), hashlib.sha256).digest()
        return self._guarded_hash(peppered, salt)

    def _target_hash(self, kind: str, value: str) -> str:
        return hmac.new(self.secret, f"{kind}\0{value}".encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def _public_user(row: sqlite3.Row | dict) -> dict:
        return {"id": row["id"], "username": row["username"], "phone": row["phone"],
                "email": row["email"], "role": "user",
                "has_security_questions": bool(row["has_security_questions"])}

    def _admin(self) -> dict:
        return {"id": "admin", "username": self.admin_user, "phone": None,
                "email": None, "role": "admin", "has_security_questions": False}

    def _issue_session(self, db: sqlite3.Connection, subject: str) -> str:
        token = secrets.token_urlsafe(32)
        timestamp = int(time.time())
        db.execute("DELETE FROM sessions WHERE expires_at <= ?", (timestamp,))
        db.execute("DELETE FROM revoked_legacy_tokens WHERE expires_at <= ?", (timestamp,))
        db.execute("INSERT INTO sessions(token_hash, subject, expires_at) VALUES (?, ?, ?)",
                   (self._token_hash(token), subject, timestamp + SESSION_LIFETIME))
        return token

    def _session_response(self, token: str, principal: dict) -> dict:
        return {"access_token": token, "token_type": "bearer", **principal, "demo": self.demo}

    @staticmethod
    def _lock_error(locked_until: int) -> HTTPException:
        remaining = max(1, locked_until - int(time.time()))
        return HTTPException(423, "失败次数过多，请 10 分钟后重试",
                             headers={"Retry-After": str(remaining)})

    @staticmethod
    def _reset_expired_lock(db: sqlite3.Connection, table: str, key_col: str,
                            key: str, row: sqlite3.Row, timestamp: int) -> sqlite3.Row:
        if row["locked_until"] and row["locked_until"] <= timestamp:
            db.execute(f"UPDATE {table} SET failed_attempts=0, locked_until=0 WHERE {key_col}=?", (key,))
            return db.execute(f"SELECT * FROM {table} WHERE {key_col}=?", (key,)).fetchone()
        return row

    def _record_failure(self, db: sqlite3.Connection, table: str, key: str,
                        current: sqlite3.Row | None, timestamp: int) -> int:
        attempts = int(current["failed_attempts"]) + 1 if current else 1
        locked_until = timestamp + LOCK_SECONDS if attempts >= MAX_FAILURES else 0
        if table == "users":
            db.execute("UPDATE users SET failed_attempts=?, locked_until=? WHERE id=?",
                       (attempts, locked_until, key))
        else:
            key_col = "username" if table == "admin_locks" else "target_hash"
            db.execute(f"""INSERT INTO {table}({key_col}, failed_attempts, locked_until)
                VALUES (?, ?, ?) ON CONFLICT({key_col}) DO UPDATE SET
                failed_attempts=excluded.failed_attempts, locked_until=excluded.locked_until""",
                       (key, attempts, locked_until))
        return locked_until

    def _rate_request(self, table: str, ip: str, target_hash: str | None,
                      target_limit: int, ip_limit: int) -> None:
        timestamp = int(time.time())
        with self._transaction() as db:
            db.execute(f"DELETE FROM {table} WHERE requested_at < ?", (timestamp - 3600,))
            ip_stats = db.execute(f"SELECT COUNT(*), MIN(requested_at) FROM {table} WHERE source_ip=?",
                                  (ip,)).fetchone()
            target_stats = db.execute(f"SELECT COUNT(*), MIN(requested_at) FROM {table} WHERE target_hash=?",
                                      (target_hash,)).fetchone() if target_hash else None
            retry = []
            if ip_stats[0] >= ip_limit:
                retry.append(ip_stats[1] + 3600 - timestamp)
            if target_stats and target_stats[0] >= target_limit:
                retry.append(target_stats[1] + 3600 - timestamp)
            if retry:
                raise HTTPException(429, "请求过于频繁，请稍后重试",
                                    headers={"Retry-After": str(max(1, max(retry)))})
            if target_hash is None:
                db.execute(f"INSERT INTO {table}(source_ip, requested_at) VALUES (?, ?)", (ip, timestamp))
            else:
                db.execute(f"INSERT INTO {table}(target_hash, source_ip, requested_at) VALUES (?, ?, ?)",
                           (target_hash, ip, timestamp))

    def _check_login_rate(self, identifier: str, ip: str) -> None:
        key = unicodedata.normalize("NFKC", identifier.strip()).casefold()
        self._rate_request("login_requests", ip, self._target_hash("login", key),
                           LOGIN_PER_IDENTIFIER_HOUR, LOGIN_PER_IP_HOUR)

    def _check_recovery_rate(self, username: str, ip: str) -> str:
        key = unicodedata.normalize("NFKC", username.strip()).casefold()
        target_hash = self._target_hash("recovery", key)
        self._rate_request("recovery_requests", ip, target_hash,
                           RECOVERY_PER_IDENTIFIER_HOUR, RECOVERY_PER_IP_HOUR)
        return target_hash

    @staticmethod
    def _validate_password(password: str) -> None:
        if not 8 <= len(password) <= 128:
            raise HTTPException(422, "密码长度须为 8 到 128 个字符")

    @staticmethod
    def _validate_questions(items: list[dict]) -> list[tuple[str, str]]:
        if len(items) != 3:
            raise HTTPException(422, "请设置恰好 3 个密保问题")
        result = []
        questions = set()
        answers = set()
        for item in items:
            question = unicodedata.normalize("NFKC", item["question"].strip())
            answer = item["answer"].strip()
            normalized = normalize_answer(answer)
            if not 5 <= len(question) <= 120 or not 1 <= len(answer) <= 128:
                raise HTTPException(422, "密保问题须为 5–120 字，答案须为 1–128 字")
            if question.casefold() in questions or normalized in answers:
                raise HTTPException(422, "三个密保问题和答案须各不相同")
            questions.add(question.casefold())
            answers.add(normalized)
            result.append((question, answer))
        return result

    def _prepare_questions(self, questions: list[tuple[str, str]]) -> list[tuple[str, str]]:
        result = []
        for _, answer in questions:
            salt = secrets.token_bytes(16)
            result.append((salt.hex(), self._answer_hash(answer, salt)))
        return result

    @staticmethod
    def _store_questions(db: sqlite3.Connection, user_id: str, questions: list[tuple[str, str]],
                         digests: list[tuple[str, str]]) -> None:
        for position, ((question, _), (salt, digest)) in enumerate(zip(questions, digests)):
            db.execute("""INSERT INTO security_questions(user_id, position, question, answer_salt, answer_hash)
                          VALUES (?, ?, ?, ?, ?)""", (user_id, position, question, salt, digest))
        db.execute("UPDATE users SET has_security_questions=1 WHERE id=?", (user_id,))

    def register(self, raw_username: str, raw_phone: str, password: str,
                 security_questions: list[dict], ip: str = "unknown") -> dict:
        username, key = normalize_username(raw_username)
        phone = normalize_phone(raw_phone)
        self._validate_password(password)
        questions = self._validate_questions(security_questions)
        if key == self.admin_user.casefold():
            raise HTTPException(409, "用户名已存在")
        self._rate_request("registration_requests", ip, None, 0, REGISTRATIONS_PER_IP_HOUR)
        with closing(self._connection()) as db:
            if db.execute("SELECT 1 FROM users WHERE username_key=? OR phone=?", (key, phone)).fetchone():
                raise HTTPException(409, "用户名或手机号已注册")
        salt = secrets.token_bytes(16)
        digest = self._guarded_password_hash(password, salt)
        answer_digests = self._prepare_questions(questions)
        user_id = f"user-{uuid.uuid4().hex}"
        try:
            with self._transaction() as db:
                db.execute("""INSERT INTO users(id, username, username_key, phone, email,
                    password_salt, password_hash, has_security_questions, created_at)
                    VALUES (?, ?, ?, ?, NULL, ?, ?, 0, ?)""",
                           (user_id, username, key, phone, salt.hex(), digest, int(time.time())))
                self._store_questions(db, user_id, questions, answer_digests)
                token = self._issue_session(db, user_id)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "用户名或手机号已注册") from exc
        return self._session_response(token, {"id": user_id, "username": username, "phone": phone,
                                             "email": None, "role": "user", "has_security_questions": True})

    def login_password(self, raw_identifier: str, password: str, ip: str = "unknown") -> dict:
        if len(raw_identifier) > 254 or len(password) > 1024:
            raise HTTPException(401, "账号或密码错误")
        self._check_login_rate(raw_identifier, ip)
        timestamp = int(time.time())
        if raw_identifier.casefold() == self.admin_user.casefold():
            with self._transaction() as db:
                row = db.execute("SELECT * FROM admin_locks WHERE username=?", (self.admin_user,)).fetchone()
                if row:
                    row = self._reset_expired_lock(db, "admin_locks", "username", self.admin_user, row, timestamp)
                    if row["locked_until"] > timestamp:
                        raise self._lock_error(row["locked_until"])
                if not hmac.compare_digest(password.encode(), self.admin_password.encode()):
                    locked = self._record_failure(db, "admin_locks", self.admin_user, row, timestamp)
                    error = self._lock_error(locked) if locked else HTTPException(401, "账号或密码错误")
                else:
                    db.execute("DELETE FROM admin_locks WHERE username=?", (self.admin_user,))
                    token = self._issue_session(db, f"admin:{self.admin_user}")
                    return self._session_response(token, self._admin())
            raise error
        key = unicodedata.normalize("NFKC", raw_identifier.strip()).casefold()
        with self._transaction() as db:
            row = db.execute("SELECT * FROM users WHERE username_key=?", (key,)).fetchone()
            if row is None and key.startswith("+86"):
                row = db.execute("SELECT * FROM users WHERE phone=?", (key,)).fetchone()
            elif row is None and PHONE_RE.fullmatch(key):
                row = db.execute("SELECT * FROM users WHERE phone=?", (f"+86{key}",)).fetchone()
            elif row is None and "@" in key:
                row = db.execute("SELECT * FROM users WHERE email=?", (key,)).fetchone()
            if row is None:
                self._guarded_password_hash(password, self._dummy_salt)
                error = HTTPException(401, "账号或密码错误")
            else:
                row = self._reset_expired_lock(db, "users", "id", row["id"], row, timestamp)
                if row["locked_until"] > timestamp:
                    raise self._lock_error(row["locked_until"])
                actual = self._guarded_password_hash(password, bytes.fromhex(row["password_salt"]))
                if not hmac.compare_digest(actual, row["password_hash"]):
                    locked = self._record_failure(db, "users", row["id"], row, timestamp)
                    error = self._lock_error(locked) if locked else HTTPException(401, "账号或密码错误")
                else:
                    db.execute("UPDATE users SET failed_attempts=0, locked_until=0 WHERE id=?", (row["id"],))
                    token = self._issue_session(db, row["id"])
                    return self._session_response(token, self._public_user(row))
        raise error

    def get_security_questions(self, raw_username: str, ip: str = "unknown") -> dict:
        if len(raw_username) > 254:
            raise HTTPException(422, "用户名过长")
        self._check_recovery_rate(raw_username, ip)
        key = unicodedata.normalize("NFKC", raw_username.strip()).casefold()
        with closing(self._connection()) as db:
            user = db.execute("SELECT id FROM users WHERE username_key=?", (key,)).fetchone()
            if not user:
                return {"questions": []}
            rows = db.execute("SELECT position, question FROM security_questions WHERE user_id=? ORDER BY position",
                              (user["id"],)).fetchall()
        return {"questions": [{"index": row["position"], "question": row["question"]} for row in rows]}

    def enroll_security_questions(self, user_id: str, items: list[dict]) -> dict:
        questions = self._validate_questions(items)
        with closing(self._connection()) as db:
            row = db.execute("SELECT has_security_questions FROM users WHERE id=?", (user_id,)).fetchone()
            if not row:
                raise HTTPException(403, "管理员账号不能设置密保问题")
            if row["has_security_questions"]:
                raise HTTPException(409, "密保问题已设置")
        digests = self._prepare_questions(questions)
        with self._transaction() as db:
            row = db.execute("SELECT has_security_questions FROM users WHERE id=?", (user_id,)).fetchone()
            if not row or row["has_security_questions"]:
                raise HTTPException(409, "密保问题已设置")
            self._store_questions(db, user_id, questions, digests)
        return {"message": "密保问题已设置"}

    @staticmethod
    def _validate_submitted_answers(items: list[dict]) -> dict[int, str]:
        if not 2 <= len(items) <= 3:
            raise HTTPException(422, "请回答至少 2 个不同的密保问题")
        result = {}
        for item in items:
            index = item["index"]
            answer = item["answer"].strip()
            if index not in (0, 1, 2) or index in result or not 1 <= len(answer) <= 128:
                raise HTTPException(422, "密保答案格式无效")
            result[index] = answer
        return result

    def reset_password(self, raw_username: str, new_password: str, answers: list[dict],
                       ip: str = "unknown", expected_user_id: str | None = None) -> dict:
        if len(raw_username) > 254:
            raise HTTPException(422, "用户名过长")
        self._validate_password(new_password)
        provided = self._validate_submitted_answers(answers)
        target_hash = self._check_recovery_rate(raw_username, ip)
        key = unicodedata.normalize("NFKC", raw_username.strip()).casefold()
        timestamp = int(time.time())
        with self._transaction() as db:
            lock = db.execute("SELECT * FROM recovery_locks WHERE target_hash=?", (target_hash,)).fetchone()
            if lock:
                lock = self._reset_expired_lock(db, "recovery_locks", "target_hash", target_hash, lock, timestamp)
                if lock["locked_until"] > timestamp:
                    raise self._lock_error(lock["locked_until"])
            user = db.execute("SELECT * FROM users WHERE username_key=?", (key,)).fetchone()
            if expected_user_id and (not user or user["id"] != expected_user_id):
                raise HTTPException(403, "账号不匹配")
            if user and user["locked_until"] > timestamp:
                raise self._lock_error(user["locked_until"])
            rows = db.execute("SELECT * FROM security_questions WHERE user_id=?",
                              (user["id"],)).fetchall() if user else []
            by_position = {row["position"]: row for row in rows}
            correct = 0
            for position, answer in provided.items():
                question = by_position.get(position)
                if question:
                    actual = self._answer_hash(answer, bytes.fromhex(question["answer_salt"]))
                    correct += hmac.compare_digest(actual, question["answer_hash"])
            if correct < 2 or not user or len(rows) != 3:
                locked = self._record_failure(db, "recovery_locks", target_hash, lock, timestamp)
                error = self._lock_error(locked) if locked else HTTPException(400, "密保验证失败")
            else:
                salt = secrets.token_bytes(16)
                digest = self._guarded_password_hash(new_password, salt)
                db.execute("""UPDATE users SET password_salt=?, password_hash=?,
                              failed_attempts=0, locked_until=0 WHERE id=?""",
                           (salt.hex(), digest, user["id"]))
                db.execute("DELETE FROM recovery_locks WHERE target_hash=?", (target_hash,))
                db.execute("DELETE FROM sessions WHERE subject=?", (user["id"],))
                return {"message": "密码已修改，请重新登录"}
        raise error

    def _legacy_admin(self, token: str) -> dict | None:
        try:
            payload, signature = token.split(".", 1)
            actual = hmac.new(self.secret, payload.encode(), hashlib.sha256).digest()
            expected = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
            body = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            if (hmac.compare_digest(actual, expected) and body["exp"] > time.time()
                    and body["sub"] == self.admin_user):
                return self._admin()
        except (ValueError, KeyError, TypeError, binascii.Error, UnicodeDecodeError):
            pass
        return None

    def current(self, token: str) -> dict:
        if not token or len(token) > 4096:
            raise HTTPException(401, "登录已失效，请重新登录")
        timestamp = int(time.time())
        with closing(self._connection()) as db:
            row = db.execute("SELECT subject, expires_at FROM sessions WHERE token_hash=?",
                             (self._token_hash(token),)).fetchone()
            if row and row["expires_at"] > timestamp:
                if row["subject"] == f"admin:{self.admin_user}":
                    return self._admin()
                user = db.execute("SELECT * FROM users WHERE id=?", (row["subject"],)).fetchone()
                if user:
                    return self._public_user(user)
            if db.execute("SELECT 1 FROM revoked_legacy_tokens WHERE token_hash=?",
                          (self._token_hash(token),)).fetchone():
                raise HTTPException(401, "登录已失效，请重新登录")
        legacy = self._legacy_admin(token)
        if legacy:
            return legacy
        raise HTTPException(401, "登录已失效，请重新登录")

    def logout(self, token: str) -> None:
        self.current(token)
        with self._transaction() as db:
            deleted = db.execute("DELETE FROM sessions WHERE token_hash=?", (self._token_hash(token),)).rowcount
            if not deleted:
                db.execute("INSERT OR REPLACE INTO revoked_legacy_tokens(token_hash, expires_at) VALUES (?, ?)",
                           (self._token_hash(token), int(time.time()) + SESSION_LIFETIME))
