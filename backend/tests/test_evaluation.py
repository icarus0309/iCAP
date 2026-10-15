"""测评引擎与接口测试：规格、判分、demo 确定性、状态机、API 契约与 live 答题契约。

不引入 pytest-asyncio：异步推进统一用 asyncio.run 包裹同步用例。
"""
import asyncio
import time
from collections import Counter

import pytest
from fastapi.testclient import TestClient

from app.evaluation import (RULER1_SPEC, RULER2_SPEC, EvaluationRunner,
                            build_result, demo_answer, flatten_cases,
                            get_cases_for_ruler, get_ruler_spec, score_case)
from app.evaluation.cases import R1_CASES, R2_CASES
from app.evaluation.client import EndpointUnavailableError
from app.main import create_app
from app.model_registry import ModelRegistry
from app.store import Store

REF_BY_PROMPT = {c["prompt"]: c["reference"]
                 for bucket in (R1_CASES, R2_CASES) for cases in bucket.values() for c in cases}
ALL_CASES = [c for bucket in (R1_CASES, R2_CASES) for cases in bucket.values() for c in cases]


# --------------------------------------------------------------------------- #
# 测试基建
# --------------------------------------------------------------------------- #
def endpoint(url="http://127.0.0.1:9001/v1", api_key="local-secret"):
    return {"url": url, "api_key": api_key, "thinking_schema": {
        "enabled": {"thinking": {"type": "enabled"}},
        "disabled": {"thinking": {"type": "disabled"}},
    }, "self_deployed": True, "max_context_length": 32768,
        "top_k": 40, "top_p": 1, "temperature": 1}


class _Harness:
    """同一个 Store 上同时挂 TestClient 与 EvaluationRunner，便于手工推进任务。"""

    def __init__(self, client, app, runner):
        self.client = client
        self.app = app
        self.runner = runner

    @property
    def store(self) -> Store:
        return self.app.state.store

    def submit(self, benchmark="Ruler1", config=None, model_id="fixture-a", dataset_id="ds-1"):
        body = {"model_id": model_id, "dataset_id": dataset_id, "benchmark": benchmark}
        if config is not None:
            body["config"] = config
        response = self.client.post("/api/evaluations", json=body)
        assert response.status_code == 201, response.text
        return response.json()

    def drain(self, task_id, live_runner=None):
        """把任务推进到终态并返回快照（上限保护）。"""
        runner = live_runner or self.runner

        async def drive():
            await runner.step()
            for _ in range(400):
                snapshot = runner.snapshot_task(task_id)
                if snapshot and snapshot["status"] in ("completed", "failed", "cancelled"):
                    return snapshot
                await runner.step()
                await asyncio.sleep(0)
            raise TimeoutError("任务未在 400 步内到达终态")

        return asyncio.run(drive())


def harness(tmp_path, monkeypatch, live=True, worker_tick=3600):
    """搭建测试环境：TestClient 用 demo=True（免生产校验）但把 tick 拉到极大，
    使后台循环不干扰；真实推进会由测试自己构造的 runner 手工驱动。

    live=True 表示 runner 走 live 答题路径（配合 FakeEvalClient 断言契约）。
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    # 固定在 demo 口径：仓库根 .env 的 DEMO_MODE / ADMIN_PASSWORD 会通过 os.getenv
    # 覆盖 create_app 的 demo 默认值，导致登录口令与默认 admin 变化
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("INNOVATION_ADMIN_USER", "admin")
    monkeypatch.setenv("INNOVATION_ADMIN_PASSWORD", "demo1234")
    monkeypatch.setenv("INNOVATION_SECRET_KEY", "test-only-secret-key-with-32-characters")
    app = create_app(tmp_path, demo=False, worker_tick=worker_tick)
    client = TestClient(app)
    client.__enter__()
    token = client.post("/api/auth/login", json={"username": "admin", "password": "demo1234"}).json()["access_token"]
    client.headers.update({"Authorization": f"Bearer {token}"})
    for model_id in ("fixture-a", "fixture-b"):
        response = client.post("/api/models", json={"id": model_id, "name": model_id,
                                                    "provider": "本地", "endpoints": [endpoint()]})
        assert response.status_code == 201, response.text
    runner = EvaluationRunner(app.state.store, ModelRegistry(app.state.store), live=live)
    return _Harness(client, app, runner)


@pytest.fixture()
def live_env(tmp_path, monkeypatch):
    """live=True 的评测环境（配合 FakeEvalClient 断端到端契约）。"""
    env = harness(tmp_path, monkeypatch, live=True)
    try:
        yield env
    finally:
        env.runner.shutdown()
        env.client.__exit__(None, None, None)


@pytest.fixture()
def demo_env(tmp_path, monkeypatch):
    """live=False 的评测环境：答题走 demo_answer，整链路无 IO。"""
    env = harness(tmp_path, monkeypatch, live=False)
    try:
        yield env
    finally:
        env.runner.shutdown()
        env.client.__exit__(None, None, None)


# --------------------------------------------------------------------------- #
# 1~5 规格、样例、判分、确定性、汇总
# --------------------------------------------------------------------------- #
def test_ruler_specs_valid():
    for spec in (RULER1_SPEC, RULER2_SPEC):
        assert len(spec.dimensions) == 5
        assert abs(sum(spec.weights()) - 1.0) < 1e-6
        assert spec.default_sample_limit >= 1
    assert get_ruler_spec("Ruler2") is RULER2_SPEC
    with pytest.raises(ValueError):
        get_ruler_spec("Ruler3")


def test_cases_unique_and_covered():
    ids = [c["id"] for c in ALL_CASES]
    assert len(ids) == 50 and len(set(ids)) == 50
    for spec, bucket in ((RULER1_SPEC, R1_CASES), (RULER2_SPEC, R2_CASES)):
        assert set(bucket) == {d["key"] for d in spec.dimensions}
        assert all(len(cases) == 5 for cases in bucket.values())
        for key, cases in bucket.items():
            assert all(c["dimension"] == key for c in cases)
            assert all(c.get("prompt") and c.get("reference") is not None for c in cases)


def test_score_case_types():
    choice = {"type": "choice", "reference": "东方红一号", "answer_keys": ["东方红"], "max_points": 2}
    assert score_case(choice, "东方红一号")["verdict"] == "pass"
    assert score_case(choice, "东方红 一号，1957")["points"] == 2
    assert score_case(choice, "不知道")["verdict"] == "fail"

    partial = {"type": "open", "reference": "查询账单", "answer_keys": ["查询", "账单"], "max_points": 2}
    assert score_case(partial, "查询")["verdict"] == "partial"
    assert score_case(partial, "查询账单费用")["verdict"] == "pass"

    math_case = {"type": "math", "reference": "42", "answer_keys": ["42"], "max_points": 2}
    assert score_case(math_case, "答案是 42。")["verdict"] == "pass"
    assert score_case(math_case, "答案是 43。")["verdict"] == "fail"

    code_case = {"type": "code", "reference": "def add(a, b):\n    return a + b",
                 "answer_keys": ["def", "return"], "max_points": 2}
    assert score_case(code_case, "def add(a, b):\n    return a + b")["verdict"] == "pass"
    assert score_case(code_case, "def add(a, b): pass")["verdict"] == "partial"
    assert score_case(code_case, "print(1)")["verdict"] == "fail"

    fmt = {"type": "format", "reference": "收到", "answer_keys": [], "max_points": 2, "max_len": 20}
    assert score_case(fmt, "收到")["verdict"] == "pass"
    assert score_case(fmt, "")["verdict"] == "fail"
    assert score_case({**fmt, "max_len": 2}, "这是一句很长的自我介绍")["verdict"] == "fail"


def test_demo_answer_deterministic():
    first = [asyncio.run(demo_answer(case)) for case in ALL_CASES]
    second = [asyncio.run(demo_answer(case)) for case in ALL_CASES]
    assert first == second
    assert all(isinstance(answer, str) and answer for answer in first)
    assert asyncio.run(demo_answer({"id": "r1-kn-003", "answer_keys": ["h2o"], "reference": "H2O"})) == \
        asyncio.run(demo_answer({"id": "r1-kn-003", "answer_keys": ["h2o"], "reference": "H2O"}))


def test_build_result_math():
    results = []
    for key in ("knowledge", "reasoning", "math", "code", "conformance"):
        results.append({"dimension": key, "points": 2, "max_points": 2, "verdict": "pass"})
    perfect = build_result(RULER1_SPEC, results)
    assert perfect["total"] == 100.0
    assert all(d["score"] == 100.0 for d in perfect["dimensions"])
    assert "优秀" in perfect["summary"]

    half = [{"dimension": r["dimension"], "points": 1, "max_points": 2, "verdict": "partial"} for r in results]
    assert build_result(RULER1_SPEC, half)["total"] == 50.0
    zero = [{"dimension": r["dimension"], "points": 0, "max_points": 2, "verdict": "fail"} for r in results]
    assert build_result(RULER1_SPEC, zero)["total"] == 0.0
    assert "有待提升" in build_result(RULER1_SPEC, zero)["summary"]


# --------------------------------------------------------------------------- #
# 6~9 状态机（直接驱动 runner）
# --------------------------------------------------------------------------- #
def test_runner_completes_demo_task(demo_env):
    task = demo_env.submit("Ruler1")
    reports_before = len(demo_env.client.get("/api/reports").json())
    snapshot = demo_env.drain(task["id"])
    assert snapshot["status"] == "completed"
    assert snapshot["progress"] == 100
    result = snapshot["result"]
    assert result["mode"] == "demo" and result["finished_at"]
    assert len(result["dimensions"]) == 5 and len(result["cases"]) == 25
    assert 0 <= result["total"] <= 100
    assert snapshot["report_id"]
    reports = demo_env.client.get("/api/reports").json()
    assert len(reports) == reports_before + 1
    report = next(r for r in reports if r["id"] == snapshot["report_id"])
    assert report["ruler_id"] == "Ruler1" and len(report["dimensions"]) == 5
    assert report["evaluation_id"] == task["id"]


def test_runner_pause_resume(demo_env):
    task = demo_env.submit("Ruler1")
    asyncio.run(demo_env.runner.step())                     # queued→running 并完成首个用例
    assert demo_env.runner.snapshot_task(task["id"])["status"] == "running"
    assert demo_env.client.patch(f"/api/evaluations/{task['id']}", json={"action": "pause"}).json()["status"] == "paused"
    first = demo_env.runner.snapshot_task(task["id"])
    asyncio.run(demo_env.runner.step())                     # paused：用例检查点原地返回
    second = demo_env.runner.snapshot_task(task["id"])
    assert second["status"] == "paused"
    assert second["progress"] == first["progress"]
    assert demo_env.client.patch(f"/api/evaluations/{task['id']}", json={"action": "resume"}).json()["status"] == "running"
    snapshot = demo_env.drain(task["id"])
    assert snapshot["status"] == "completed" and len(snapshot["result"]["cases"]) == 25


def test_runner_cancel(demo_env):
    task = demo_env.submit("Ruler1")
    asyncio.run(demo_env.runner.step())
    reports_before = len(demo_env.client.get("/api/reports").json())
    assert demo_env.client.patch(f"/api/evaluations/{task['id']}", json={"action": "cancel"}).json()["status"] == "cancelled"
    asyncio.run(demo_env.runner.step())
    snapshot = demo_env.runner.snapshot_task(task["id"])
    assert snapshot["status"] == "cancelled"
    assert snapshot["report_id"] is None and snapshot.get("result") is None
    assert len(demo_env.client.get("/api/reports").json()) == reports_before
    assert any(("cancel" in log.lower()) or ("已取消" in log) for log in snapshot["logs"])


def test_sync_scores_ruler1_only(demo_env):
    ruler1 = demo_env.submit("Ruler1", config={"sample_limit": 1, "sync_scores": True})
    demo_env.drain(ruler1["id"])
    scores = next(m for m in demo_env.store.snapshot()["models"] if m["id"] == "fixture-a")["scores"]
    assert len(scores) == 5

    ruler2 = demo_env.submit("Ruler2", config={"sample_limit": 1, "sync_scores": True}, model_id="fixture-b")
    demo_env.drain(ruler2["id"])
    scores_b = next(m for m in demo_env.store.snapshot()["models"] if m["id"] == "fixture-b")["scores"]
    assert scores_b == []


# --------------------------------------------------------------------------- #
# 10~13 / 16 API 契约
# --------------------------------------------------------------------------- #
def test_api_rulers_endpoint(live_env):
    response = live_env.client.get("/api/rulers")
    assert response.status_code == 200
    rulers = response.json()["rulers"]
    assert [r["id"] for r in rulers] == ["Ruler1", "Ruler2"]
    for ruler in rulers:
        assert len(ruler["dimensions"]) == 5
        assert ruler["available_cases"] == 25
        assert abs(sum(d["weight"] for d in ruler["dimensions"]) - 1.0) < 1e-6


def test_api_create_ruler_task(live_env):
    task = live_env.submit("Ruler1", config={"sample_limit": 2, "sync_scores": True})
    assert task["ruler_label"] == "尺子一测评"
    assert task["config"] == {"sample_limit": 2, "sample_limits": None,
                              "max_tokens": 1024, "temperature": 0.0, "sync_scores": True}
    assert task["result"] is None and task["status"] == "queued"

    default_task = live_env.submit("Ruler2")
    assert default_task["ruler_label"] == "尺子二测评"
    assert default_task["config"]["sample_limit"] == RULER2_SPEC.default_sample_limit
    assert default_task["config"]["sync_scores"] is False

    assert live_env.client.post("/api/evaluations", json={"model_id": "fixture-a",
        "dataset_id": "ds-1", "benchmark": "Ruler9"}).status_code == 422
    assert live_env.client.post("/api/evaluations", json={"model_id": "missing",
        "dataset_id": "ds-1", "benchmark": "Ruler1"}).status_code == 404


def test_per_dimension_sample_limits(demo_env):
    """逐维度取样：每个维度用自己的条数，未列出的维度回落 sample_limit。"""
    config = {"sample_limit": 1, "sample_limits": {"knowledge": 3, "math": 2}, "sync_scores": False}
    task = demo_env.submit("Ruler1", config=config)
    assert task["config"]["sample_limits"] == {"knowledge": 3, "math": 2}
    snapshot = demo_env.drain(task["id"])
    assert snapshot["status"] == "completed"
    cases = snapshot["result"]["cases"]
    # 3 + 1(推理) + 2 + 1(代码) + 1(指令遵循) = 8
    assert len(cases) == 8
    per_dim = Counter(row["dimension"] for row in cases)
    assert per_dim["knowledge"] == 3 and per_dim["math"] == 2
    assert per_dim["reasoning"] == 1 and per_dim["code"] == 1 and per_dim["conformance"] == 1
    # 维度分仍按该维度实际满分归一，缺失取整不会失真
    assert all(0 <= dim["score"] <= 100 for dim in snapshot["result"]["dimensions"])


def test_per_dimension_limits_validation(live_env):
    """非法维度 key、越界条数、非整数值均应在请求期 422，不落库。"""
    base = {"model_id": "fixture-a", "dataset_id": "ds-1", "benchmark": "Ruler1"}
    for limits in ({"knowledge": 0}, {"knowledge": 11}, {"knowledge": "2"}, {"nope": 1}):
        response = live_env.client.post("/api/evaluations", json={**base, "config": {"sample_limits": limits}})
        assert response.status_code == 422, (limits, response.text)
    before = len(live_env.client.get("/api/evaluations").json())
    live_env.client.post("/api/evaluations", json={**base, "config": {"sample_limits": {"nope": 1}}})
    assert len(live_env.client.get("/api/evaluations").json()) == before


def test_batch_ignores_per_dimension_limits(live_env):
    """batch 接口不接收 config：sample_limits 被忽略，子任务用尺子默认值。"""
    response = live_env.client.post("/api/evaluations/batch", json={
        "model_ids": ["fixture-a"], "dataset_id": "ds-1", "benchmark": "Ruler2",
        "config": {"sample_limit": 1, "sample_limits": {"memory": 1}}})
    assert response.status_code == 201
    config = response.json()[0]["config"]
    assert config["sample_limits"] is None
    assert config["sample_limit"] == RULER2_SPEC.default_sample_limit


def test_api_result_endpoint_states(demo_env):
    task = demo_env.submit("Ruler1", config={"sample_limit": 1})
    asyncio.run(demo_env.runner.step())
    pending = demo_env.client.get(f"/api/evaluations/{task['id']}/result").json()
    assert pending["result"] is None and pending["state"] == "running" and "progress" in pending
    demo_env.drain(task["id"])
    done = demo_env.client.get(f"/api/evaluations/{task['id']}/result").json()
    assert done["state"] == "completed" and done["result"]["total"] is not None
    assert len(done["result"]["cases"]) == 5          # 5 维度 × sample_limit 1
    assert demo_env.client.get("/api/evaluations/nope/result").status_code == 404


def test_background_worker_completes_demo_task(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("DEMO_MODE", "true")           # 免生产校验
    monkeypatch.setenv("INNOVATION_ADMIN_USER", "admin")
    monkeypatch.setenv("INNOVATION_ADMIN_PASSWORD", "demo1234")   # 登录口令固定为 demo 默认值
    app = create_app(tmp_path, demo=True)             # demo → worker_tick=0.05
    with TestClient(app) as client:
        token = client.post("/api/auth/login", json={"username": "admin", "password": "demo1234"}).json()["access_token"]
        client.headers.update({"Authorization": f"Bearer {token}"})
        assert client.post("/api/models", json={"id": "fixture-a", "name": "fixture-a",
            "provider": "本地", "endpoints": [endpoint()]}).status_code == 201
        task = client.post("/api/evaluations", json={"model_id": "fixture-a",
            "dataset_id": "ds-1", "benchmark": "Ruler1"}).json()
        deadline = time.monotonic() + 20
        status = task
        while time.monotonic() < deadline:
            status = client.get(f"/api/evaluations/{task['id']}").json()
            if status["status"] in ("completed", "failed"):
                break
            time.sleep(0.1)
        assert status["status"] == "completed", status["logs"]
        assert status["result"]["total"] is not None
        reports = [r for r in client.get("/api/reports").json() if r["evaluation_id"] == task["id"]]
        assert len(reports) == 1 and reports[0]["ruler_id"] == "Ruler1"


def test_batch_uses_defaults(live_env):
    response = live_env.client.post("/api/evaluations/batch", json={"model_ids": ["fixture-a", "fixture-b"],
        "dataset_id": "ds-1", "benchmark": "Ruler1"})
    assert response.status_code == 201
    tasks = response.json()
    assert len(tasks) == 2
    for task in tasks:
        assert task["ruler_label"] == "尺子一测评"
        assert task["config"] == {"sample_limit": RULER1_SPEC.default_sample_limit, "sample_limits": None,
                                  "max_tokens": 1024, "temperature": 0.0, "sync_scores": False}
        assert task["result"] is None
    # batch 契约不接受 config：多余字段被忽略，仍用默认参数
    ignored = live_env.client.post("/api/evaluations/batch", json={"model_ids": ["fixture-a"],
        "dataset_id": "ds-1", "benchmark": "Ruler2", "config": {"sample_limit": 1, "sync_scores": True}})
    assert ignored.status_code == 201
    assert ignored.json()[0]["config"]["sample_limit"] == RULER2_SPEC.default_sample_limit
    assert ignored.json()[0]["config"]["sync_scores"] is False
    assert live_env.client.post("/api/evaluations/batch", json={"model_ids": ["fixture-a", "fixture-a"],
        "dataset_id": "ds-1", "benchmark": "Ruler1"}).status_code == 422


def test_legacy_benchmark_still_simulated(live_env):
    """非尺子 benchmark 仍由模拟执行器推进（不进入测评引擎），保持旧行为。"""
    task = live_env.submit("ASR")
    assert "ruler_label" not in task
    from app.main import simulate_legacy_evaluations
    for _ in range(12):
        simulate_legacy_evaluations(live_env.store)
        if live_env.store.snapshot()["evaluations"][0]["status"] == "completed":
            break
    completed = live_env.store.snapshot()["evaluations"][0]
    assert completed["status"] == "completed"
    assert completed["report_id"]
    assert completed.get("result") is None
    reports = [r for r in live_env.client.get("/api/reports").json() if r["evaluation_id"] == task["id"]]
    assert len(reports) == 1 and reports[0]["simulated"] is True
    # 走引擎的尺子任务不会被模拟器推进
    ruler_task = live_env.submit("Ruler1", config={"sample_limit": 1})
    simulate_legacy_evaluations(live_env.store)
    assert live_env.store.snapshot()["evaluations"][0]["status"] == "queued"
    assert ruler_task["status"] == "queued"


# --------------------------------------------------------------------------- #
# 14~15 live 答题契约与失败路径
# --------------------------------------------------------------------------- #
class FakeEvalUpstream:
    """与 httpx.Response 对齐的最小对象。"""

    def __init__(self, content, status_code=200):
        self.status_code = status_code
        self._json = {"choices": [{"message": {"content": content}}]}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise EndpointUnavailableError(f"HTTP {self.status_code}")


class FakeEvalClient:
    """替换 httpx.AsyncClient：按 prompt 反查参考答案，判分必然 pass。"""

    calls: list = []

    def __init__(self, **kwargs):
        self.calls = FakeEvalClient.calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, headers=None, json=None):
        self.calls.append((url, headers, json))
        prompt = json["messages"][-1]["content"]
        return FakeEvalUpstream(REF_BY_PROMPT[prompt])


def test_live_answer_contract(live_env, monkeypatch):
    FakeEvalClient.calls = []
    monkeypatch.setattr("app.evaluation.client.httpx.AsyncClient", FakeEvalClient)
    task = live_env.submit("Ruler1", config={"sample_limit": 1, "max_tokens": 256, "temperature": 0.0})
    snapshot = live_env.drain(task["id"])
    assert snapshot["status"] == "completed"
    result = snapshot["result"]
    assert result["mode"] == "live"
    assert len(result["cases"]) == 5
    assert all(row["verdict"] == "pass" for row in result["cases"])
    assert result["total"] == 100.0
    assert len(FakeEvalClient.calls) == 5
    url, headers, payload = FakeEvalClient.calls[0]
    assert url == "http://127.0.0.1:9001/v1/chat/completions"
    assert headers["Authorization"] == "Bearer local-secret"
    assert payload["stream"] is False and payload["temperature"] == 0.0 and payload["max_tokens"] == 256
    assert payload["model"] == "fixture-a"
    assert payload["messages"][1]["content"] in REF_BY_PROMPT


def test_failed_when_endpoint_down(live_env):
    """端点不可达：重试 2 次后任务置 failed，不生成报告。"""
    assert live_env.client.post("/api/models", json={"id": "broken", "name": "broken", "provider": "本地",
        "endpoints": [endpoint("http://127.0.0.1:1/v1")]}).status_code == 201
    task = live_env.submit("Ruler1", config={"sample_limit": 1}, model_id="broken")
    snapshot = asyncio.run(_drive(live_env.runner, task["id"]))
    assert snapshot["status"] == "failed"
    assert snapshot["report_id"] is None
    assert any("执行失败" in log for log in snapshot["logs"])
    reports = [r for r in live_env.client.get("/api/reports").json() if r["evaluation_id"] == task["id"]]
    assert reports == []


async def _drive(runner, task_id):
    await runner.step()
    for _ in range(60):
        snapshot = runner.snapshot_task(task_id)
        if snapshot and snapshot["status"] in ("completed", "failed", "cancelled"):
            return snapshot
        await runner.step()
        await asyncio.sleep(0)
    raise TimeoutError("任务未在 60 步内到达终态")


def test_restart_resumes_queued_task(demo_env):
    """进程重启后 running 任务无内存 session → 重新排队并最终完成。"""
    task = demo_env.submit("Ruler1", config={"sample_limit": 1})
    asyncio.run(demo_env.runner.step())
    assert demo_env.runner.snapshot_task(task["id"])["status"] == "running"
    demo_env.runner.shutdown()                       # 模拟进程重启：内存 session 丢失
    asyncio.run(demo_env.runner.step())              # 触发重启重排
    assert any("进程重启" in log for log in demo_env.runner.snapshot_task(task["id"])["logs"])
    snapshot = demo_env.drain(task["id"])
    assert snapshot["status"] == "completed"


def test_shutdown_stop_event_does_not_block(demo_env):
    """应用关闭（stop 置位）时 runner.step 立即返回并挂起任务，不阻塞 lifespan。"""
    demo_env.runner.stop.set()
    task = demo_env.submit("Ruler1", config={"sample_limit": 1})
    started = time.monotonic()
    asyncio.run(demo_env.runner.step())
    assert time.monotonic() - started < 5
    snapshot = demo_env.runner.snapshot_task(task["id"])
    assert snapshot["status"] == "running"
    assert snapshot["progress"] == 0 and snapshot["result"] is None
    assert any("应用关闭" in log for log in snapshot["logs"])
    assert demo_env.runner.active_task_id is None


def test_flatten_cases_limit():
    flat = flatten_cases(get_cases_for_ruler("Ruler1"), 2)
    assert len(flat) == 10
    assert [dim for dim, _ in flat[:2]] == ["knowledge", "knowledge"]
    assert len(flatten_cases(get_cases_for_ruler("Ruler2"), None)) == 25
    with pytest.raises(ValueError):
        flatten_cases(get_cases_for_ruler("Ruler1"), 0)
    with pytest.raises(ValueError):
        get_cases_for_ruler("Ruler9")


def test_flatten_cases_per_dimension():
    flat = flatten_cases(get_cases_for_ruler("Ruler1"), {"knowledge": 3, "math": 2})
    per_dim = Counter(dim for dim, _ in flat)
    assert per_dim["knowledge"] == 3 and per_dim["math"] == 2
    assert per_dim["reasoning"] == 5 and per_dim["code"] == 5 and per_dim["conformance"] == 5  # 未列出 → 全部
    assert len(flat) == 20
    # 顺序仍为规格维度顺序
    assert [dim for dim, _ in flat[:3]] == ["knowledge"] * 3
    # 条数超过实际样例数 → 按实际执行，不报错
    assert len(flatten_cases(get_cases_for_ruler("Ruler2"), {"memory": 99})) == 25
    # 空 dict 等价于全部
    assert len(flatten_cases(get_cases_for_ruler("Ruler1"), {})) == 25
    for bad in ({"knowledge": 0}, {"knowledge": -1}, {"knowledge": True}, {"knowledge": "2"}):
        with pytest.raises(ValueError):
            flatten_cases(get_cases_for_ruler("Ruler1"), bad)
