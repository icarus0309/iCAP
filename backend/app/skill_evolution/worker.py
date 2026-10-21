"""One selected upstream engine per subprocess; stdin is the only credential channel."""
from __future__ import annotations

import dataclasses
import json
import math
import os
import re
import shutil
import sys
import threading
import time
import zipfile
from pathlib import Path

import httpx


def event(**data):
    print("ICAP_EVENT:" + json.dumps(data, ensure_ascii=False), flush=True)


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {} if default is None else default


def write(path, data):
    path = Path(path)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def build_report(job_dir, cfg, final=False):
    outputs = job_dir / "outputs"
    runs = sorted(p for p in outputs.glob("*") if p.is_dir()) if outputs.exists() else []
    run = runs[-1] if runs else None
    rounds = []
    if run:
        for rd in sorted(run.glob("round_*"), key=lambda p: int(p.name.split("_")[-1])):
            summary = read(rd / "round_summary.json")
            cases = []
            for cd in sorted((rd / "cases").glob("*")):
                if not cd.is_dir():
                    continue
                score, analysis, result = read(cd / "score.json"), read(cd / "session_analysis.json"), read(cd / "run_result.json")
                if not result and not score:
                    continue
                route = read(cd / "route.json")
                cases.append({"id": cd.name, "score": score.get("total_score"), "result_score": score.get("result"),
                              "route": route.get("route", "INVALID" if not score else "SUCCESS" if score.get("total_score", 0) >= cfg["outcome_split_threshold"] else "FAILURE"),
                              "output": read(cd / "session.json").get("final_output") or result.get("final_output", ""), "feedback": score.get("feedback", ""),
                              "process_score": score.get("process") if cfg["execution_mode"] == "hermes" else None,
                              "conciseness_score": score.get("conciseness") if cfg["execution_mode"] == "hermes" else None,
                              "trajectory": read(cd / "trajectory.json", []) if cfg["execution_mode"] == "hermes" else [],
                              "large_trajectory": read(cd / "large" / "trajectory.json", []) if cfg["execution_mode"] == "hermes" else [],
                              "analysis": analysis.get("output_analyses", {}), "error": score.get("evaluation_error") or result.get("error", ""),
                              "large_output": read(cd / "large" / "run_result.json").get("final_output", ""),
                              "knowledge": read(cd / "knowledge" / "knowledge.json")})
            rounds.append({"index": int(rd.name.split("_")[-1]), "score": summary.get("average_score"),
                           "cases": cases, "summary": summary,
                           "experience": read(rd / "evaluation.json"),
                           "mutation": read(rd / "mutation_plan.json"),
                           "exploration": read(rd / "direct_exploration" / "checkpoint.json")})
    scored = [r for r in rounds if isinstance(r["score"], (int, float))]
    report = {"rounds": rounds, "best_score": max((r["score"] for r in scored), default=None),
              "initial_score": scored[0]["score"] if scored else None,
              "final_score": scored[-1]["score"] if scored else None,
              "experience_pool": read(run / "experience_pool.json") if run else {},
              "lineage": read(run / "lineage.json", []) if run else [],
              "artifacts": [p.name for p in job_dir.iterdir() if p.name in {
                  "optimized_skill.md", "final_skill.md", "initial_skill.md", "report.json", "artifacts.zip"}],
              "engine": cfg["engine"], "operation": cfg["operation"]}
    if final and run:
        summary = read(run / "summary.json") or read(run / "self_evolve_summary.json")
        if summary.get("best_score") is not None:
            report["best_score"] = summary["best_score"]
        for name in ("optimized_skill.md", "final_skill.md", "initial_skill.md"):
            if (run / name).is_file():
                shutil.copyfile(run / name, job_dir / name)
    if (job_dir / "optimized_skill.md").exists():
        report["optimized_skill"] = (job_dir / "optimized_skill.md").read_text(encoding="utf-8")
    return report


def patch_opt_judge(file_only=False):
    """Translate the platform rubric contract into OPT's analyzer contract.

    Upstream OPT silently treats {score: 0} as a pass (missing issues). Reject
    unknown verdicts and honor explicit numeric scores instead.
    """
    from src.self_evolve.evaluators.components.session_analyzer import SessionAnalyzer

    def analyze(self, *, trajectory, input_text, input_messages, rubric_path):
        template = Path(rubric_path).read_text(encoding="utf-8")
        output = next((s.content for s in reversed(trajectory) if s.role == "assistant"), "")
        values = {"conversation": self._build_conversation(input_messages, input_text),
                  "input_messages": json.dumps(input_messages, ensure_ascii=False),
                  "input": input_text, "candidate_output": output, "memory_list": output,
                  "model_output": output, "assistant_output": output,
                  "skill_prompt": os.environ.get("ICAP_SKILL_CONTEXT", ""),
                  "skill_excerpt": os.environ.get("ICAP_SKILL_CONTEXT", "")[:7000],
                  "current_year": str(time.localtime().tm_year), "history_memory_list": "无", "peer_memory_list": "无", "fewshots": "无"}
        def replacement(match):
            if match[1] not in values:
                raise ValueError("未知的评审模板占位符：" + match[1])
            return values[match[1]]
        prompt = re.sub(r"(?<!\{)\{([A-Za-z_][A-Za-z_0-9]*)\}(?!\})", replacement, template)
        verdict = self._parse_json(self.model_client.generate([{"role": "user", "content": prompt}]))
        if not isinstance(verdict, dict):
            raise ValueError("评审模型未返回 JSON 对象")
        if "issues" in verdict:
            if not isinstance(verdict["issues"], list):
                raise ValueError("评审 issues 必须是数组")
            score = 0.0 if verdict["issues"] else 100.0
        elif "审核结果" in verdict:
            score = 100.0 if verdict["审核结果"] == "合规" else 0.0
        else:
            score = verdict.get("score")
            if isinstance(score, bool) or not isinstance(score, (float, int)) or not math.isfinite(score) or not 0 <= score <= 100:
                raise ValueError("评审模型未返回有效的 0–100 分数或判定")
        return float(score), {**verdict, "score": score, "passed": score >= 80, "analysis": verdict.get("reason", ""), "source": "model"}

    if not file_only:
        SessionAnalyzer._analyze_onlyllm = analyze

    def analyze_file(self, *, expected_file):
        expected = Path(expected_file["expect_path"]).read_text(encoding="utf-8")
        actual = os.environ.get("ICAP_LAST_OUTPUT", "")
        if os.environ.get("ICAP_HERMES_WORKSPACE"):
            root = Path(os.environ["ICAP_HERMES_WORKSPACE"]).resolve()
            path = (root / expected_file.get("actual_path", "result.txt")).resolve()
            if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > 2_000_000:
                return 0.0, {"analysis": "实际文件缺失、超出任务目录或超过 2 MB", "source": "file"}
            actual = path.read_text(encoding="utf-8", errors="replace")
        prompt = "比较候选输出与期望文件内容的匹配度。只输出JSON，含0–100的score和analysis字段。\n期望内容：\n" + expected + "\n候选输出：\n" + actual
        verdict = self._parse_json(self.model_client.generate([{"role": "user", "content": prompt}]))
        score = verdict.get("score") if isinstance(verdict, dict) else None
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 100:
            raise ValueError("文件内容评审未返回有效的0–100分数")
        return float(score), {"score": score, "analysis": verdict.get("analysis", ""), "source": "model"}
    SessionAnalyzer._analyze_file = analyze_file


def main(payload):
    cfg, models = payload["config"], payload["models"]
    job_dir = Path(payload["job_dir"]).resolve()
    assets = job_dir / "assets"
    assets.mkdir(exist_ok=True)
    (job_dir / "outputs").mkdir(exist_ok=True)
    for resource in cfg["resources"]:
        target = assets / resource["name"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(resource["content"], encoding="utf-8")
    (assets / "SKILL.md").write_text(cfg["skill_content"], encoding="utf-8")
    (assets / "rubric.md").write_text(cfg["rubric"], encoding="utf-8")
    dataset = cfg["dataset"]
    for item in dataset:
        for index, step in enumerate(item.get("key_steps", [])):
            step["step_id"] = str(step.get("step_id", step.get("id", step.get("name", index + 1))))
        # OPT only exposes text entries to Hermes; enhanced already renders its
        # JSON conversation. Adding text there would duplicate the question.
        if cfg["execution_mode"] == "hermes" and cfg["engine"] == "opt" and not any(entry.get("type") == "text" for entry in item["input"]):
            messages = []
            for entry in item["input"]:
                if entry.get("type") in {"json", "list"}:
                    data = entry["data"]
                    messages.extend(json.loads(data) if isinstance(data, str) else data)
            item["input"].append({"type": "text", "data": "\n\n".join(f"[{m['role']}]\n{m['content']}" for m in messages)})
        for output in item.get("output", []):
            if output["type"] == "onlyllm":
                output["data"] = str((assets / output["data"]).resolve())
            if output["type"] == "file":
                output["expect_path"] = str((assets / output["expect_path"]).resolve())
                output.setdefault("actual_path", "")
    write(assets / "dataset.json", dataset)
    engine_dir = Path(__file__).resolve().parents[2] / "vendor" / "skill_evolution" / cfg["engine"]
    sys.path.insert(0, str(engine_dir))
    os.chdir(assets)
    for key, value in cfg["output_weights"].items():
        os.environ["OUTPUT_WEIGHT_" + key.upper()] = str(value)
    if cfg["execution_mode"] == "direct":
        os.environ.update(JUDGMENT_WEIGHT_PROCESS="0", JUDGMENT_WEIGHT_CONCISENESS="0", JUDGMENT_WEIGHT_RESULT="1")
    os.environ["OUTPUT_FORMAT_EXAMPLE"] = cfg["format_example"]
    os.environ["ICAP_SKILL_CONTEXT"] = cfg["skill_content"]
    import src.config as source_config
    source_config.load_dotenv = lambda *args, **kwargs: None
    source_config._DOTENV_LOADED = True
    from src.config import ModelConfig

    calls = payload.get("initial_model_calls", 0)
    call_lock = threading.Lock()
    fatal_errors = []
    max_calls = cfg["max_model_calls"]
    class Client:
        def __init__(self, model, role):
            self.model, self.role, self.index = model, role, 0
            self.config = ModelConfig(model_name=model["name"], api_base=model["endpoints"][0]["url"], api_key=None,
                                      temperature=cfg["temperature"] if role != "eval" else 0.1,
                                      max_tokens=cfg["max_tokens"], timeout=cfg["timeout"])

        def generate_with_system(self, system_prompt, user_prompt, **kwargs):
            return self.generate([{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}], **kwargs)

        def generate(self, messages, **kwargs):
            nonlocal calls
            if self.role == "exec" and messages and messages[0].get("role") == "system":
                os.environ["ICAP_SKILL_CONTEXT"] = messages[0].get("content", "")
            endpoint = self.model["endpoints"][self.index % len(self.model["endpoints"])]
            self.index += 1
            base = endpoint["url"].rstrip("/")
            url = base if base.endswith("/chat/completions") else base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")
            body = {"model": self.config.model_name, "messages": messages, "stream": False,
                    "max_tokens": self.config.max_tokens, "temperature": self.config.temperature}
            body.update(endpoint.get("thinking_schema", {}).get("disabled", {}))
            if "qwen3" in self.config.model_name.lower():
                body["enable_thinking"] = False
            for key, value in kwargs.get("extra_params", {}).items():
                if key not in {"model", "messages", "stream"}:
                    body[key] = value
            for attempt in range(2):
                with call_lock:
                    if calls >= max_calls:
                        fatal_errors.append("已达到本任务模型调用预算")
                        raise RuntimeError("已达到本任务模型调用预算，请调整预算后新建任务")
                    calls += 1
                    event(model_calls=calls, phase=f"{self.role} 模型调用中")
                try:
                    with httpx.Client(timeout=self.config.timeout, trust_env=False) as client:
                        response = client.post(url, json=body, headers={"Authorization": "Bearer " + endpoint["api_key"]})
                    if response.status_code in {429, 500, 502, 503, 504} and attempt == 0:
                        time.sleep(2)
                        continue
                    if response.status_code >= 400:
                        fatal_errors.append(f"{self.role} 模型请求失败 HTTP {response.status_code}")
                        raise RuntimeError(f"{self.role} 模型请求失败 HTTP {response.status_code}，请检查模型名称、服务地址或账户额度")
                    result = response.json()
                    content = result["choices"][0]["message"].get("content")
                    if not isinstance(content, str) or not content.strip():
                        raise RuntimeError(f"{self.role} 模型返回空内容，可能输出长度不足")
                    for m in models.values():
                        for ep in m["endpoints"]:
                            if ep.get("api_key"):
                                content = content.replace(ep["api_key"], "[密钥已隐藏]")
                    if self.role == "exec":
                        os.environ["ICAP_LAST_OUTPUT"] = content
                    return content
                except httpx.RequestError:
                    if attempt == 0:
                        continue
                    fatal_errors.append(f"{self.role} 模型连接超时或网络不可达")
                    raise RuntimeError(f"{self.role} 模型连接超时或网络不可达") from None
                except (ValueError, KeyError, IndexError):
                    raise RuntimeError(f"{self.role} 模型响应不符合 Chat Completions 格式") from None

    clients = {role: Client(model, role) for role, model in models.items()}
    relay = None
    if cfg["execution_mode"] == "hermes" and cfg["operation"] != "bootstrap":
        from .hermes import InferenceRelay, install_runner

        def hermes_request(role, request_body):
            nonlocal calls
            client = clients[role]
            with call_lock:
                if calls >= max_calls:
                    fatal_errors.append("已达到本任务模型调用预算")
                    raise RuntimeError(fatal_errors[-1])
                calls += 1
                endpoint = client.model["endpoints"][client.index % len(client.model["endpoints"])]
                client.index += 1
                event(model_calls=calls, phase=f"Hermes {role} 工具执行中")
            base = endpoint["url"].rstrip("/")
            url = base if base.endswith("/chat/completions") else base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")
            body = {key: request_body[key] for key in ("messages", "tools", "tool_choice", "parallel_tool_calls", "temperature", "top_p", "stop") if key in request_body}
            body.update(model=client.model["name"], stream=False, max_tokens=cfg["max_tokens"], temperature=cfg["temperature"])
            body.update(endpoint.get("thinking_schema", {}).get("disabled", {}))
            if "qwen3" in client.model["name"].lower():
                body["enable_thinking"] = False
            try:
                with httpx.Client(timeout=cfg["timeout"], trust_env=False) as http:
                    response = http.post(url, json=body, headers={"Authorization": "Bearer " + endpoint["api_key"]})
                if response.status_code >= 400:
                    raise RuntimeError(f"Hermes {role} 模型请求失败 HTTP {response.status_code}")
                data = response.json()
                if not data.get("choices"):
                    raise RuntimeError("Hermes 模型未返回有效结果")
                encoded = json.dumps(data, ensure_ascii=False)
                for model in models.values():
                    for ep in model["endpoints"]:
                        encoded = encoded.replace(ep["api_key"], "[密钥已隐藏]")
                return json.loads(encoded)
            except Exception as exc:
                error = str(exc) if isinstance(exc, RuntimeError) else "Hermes 模型连接失败或响应无效"
                fatal_errors.append(error)
                raise RuntimeError(error) from None

        relay = InferenceRelay(hermes_request, {role: models[role] for role in ("exec", "large") if role in models})
        install_runner(relay, cfg, models, assets, fatal_errors)
    event(progress=3, phase="准备运行")
    if cfg["operation"] == "bootstrap":
        skill = clients["gen"].generate([
            {"role": "system", "content": "请根据业务需求生成可直接使用的 SKILL.md，含 YAML frontmatter 的 name、description，清晰的任务流程、边界、输出要求和例子。只返回 Markdown，不要代码围栏，不要生成可执行脚本。"},
            {"role": "user", "content": cfg["requirement"]}])
        if skill.strip().startswith("```"):
            skill = skill.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        for name in ("optimized_skill.md", "final_skill.md"):
            (job_dir / name).write_text(skill, encoding="utf-8")
        report = build_report(job_dir, cfg)
    else:
        from src.self_evolve.orchestrator import SelfEvolveConfig, SelfEvolveOrchestrator
        from src.self_evolve.execution.hermes_runner import HermesCliConfig
        from src.self_evolve.schemas import ScorerWeights
        from src.self_evolve.evaluators.outcome_grouped_evaluator import OutcomeGroupedEvaluator
        original_evaluate = OutcomeGroupedEvaluator.evaluate
        def capture_evaluation(evaluator, **kwargs):
            result = original_evaluate(evaluator, **kwargs)
            runs = sorted((job_dir / "outputs").glob("*"))
            if runs:
                index = kwargs.get("state", {}).get("round_index", 1)
                write(runs[-1] / f"round_{index}" / "evaluation.json", dataclasses.asdict(result))
            return result
        OutcomeGroupedEvaluator.evaluate = capture_evaluation
        rounds = 1 if cfg["operation"] == "evaluate" else cfg["rounds"]
        common = dict(rounds=rounds, threshold=cfg["threshold"], cases_limit=cfg["cases_limit"],
                      output_dir=str(job_dir / "outputs"), exec_mode=cfg["execution_mode"],
                      hermes=HermesCliConfig(hermes_bin=os.getenv("HERMES_BIN", "hermes"), seed_user_config=False,
                                            hermes_model=models["exec"]["name"], hermes_timeout=cfg["timeout"]))
        existing = sorted((job_dir / "outputs").glob("*"))
        resume = str(existing[-1]) if payload.get("resume") and existing else None
        if cfg["engine"] == "opt":
            patch_opt_judge()
            config = SelfEvolveConfig(**common, resume_dir=resume, structured_mutator_max_steps=cfg["max_edits"],
                                     outcome_split_threshold=cfg["outcome_split_threshold"],
                                     enable_rejected_experience=cfg["rejected_experience"],
                                     success_case_limit=cfg["success_case_limit"], failure_case_limit=cfg["failure_case_limit"],
                                     scorer_weights=ScorerWeights(), output_weights=cfg["output_weights"])
            orchestrator = SelfEvolveOrchestrator(config=config, exec_model_client=clients["exec"],
                                                 eval_model_client=clients["eval"], gen_model_client=clients["gen"])
        else:
            patch_opt_judge(file_only=True)
            if relay and "large" in models:
                common["large_hermes"] = HermesCliConfig(seed_user_config=False, hermes_model=models["large"]["name"],
                                                         hermes_env={"ICAP_MODEL_ROLE": "large"})
            config = SelfEvolveConfig(**common, exec_model_client=clients["exec"], large_exec_model_client=clients.get("large"),
                                     direct_judge_prompt=str(assets / "rubric.md"), result_threshold=cfg["result_threshold"],
                                     outcome_split_threshold=cfg["outcome_split_threshold"], max_steps=cfg["max_edits"],
                                     stall_threshold=cfg["stall_threshold"], stall_rounds=cfg["stall_rounds"],
                                     gepa_iterations=cfg["exploration_iterations"], gepa_max_metric_calls=max_calls)
            orchestrator = SelfEvolveOrchestrator(config=config, eval_model_client=clients["eval"], gen_model_client=clients["gen"])
        done = threading.Event()
        def monitor():
            while not done.wait(1):
                try:
                    report = build_report(job_dir, cfg)
                    write(job_dir / "live.json", report)
                    active = report["rounds"][-1] if report["rounds"] else None
                    if active:
                        total = min(len(dataset), cfg["cases_limit"] or len(dataset))
                        fraction = sum(c["score"] is not None for c in active["cases"]) / max(1, total)
                        event(current_round=active["index"], progress=min(95, int(5 + 90 * (active["index"] - 1 + fraction) / rounds)))
                except Exception:
                    pass
        monitor_thread = threading.Thread(target=monitor, daemon=True)
        monitor_thread.start()
        try:
            if cfg["engine"] == "opt":
                orchestrator.run(str(assets / "SKILL.md"), str(assets / "dataset.json"))
            else:
                orchestrator.run(str(assets / "SKILL.md"), str(assets / "dataset.json"), resume_from=resume)
        finally:
            done.set()
            monitor_thread.join(timeout=3)
            if relay:
                relay.close()
        report = build_report(job_dir, cfg, final=True)
        if fatal_errors:
            raise RuntimeError(fatal_errors[-1] + "；已保留已完成的用例，可检查配置后续跑或新建任务")
        valid = [c for r in report["rounds"] for c in r["cases"] if c["score"] is not None and not c["error"] and c["route"] != "INVALID"]
        if not valid:
            raise RuntimeError("全部用例执行或评审失败，请检查模型和评审规则。详细原因见用例记录。")
    report["model_calls"] = calls
    report["artifacts"] = ["optimized_skill.md", "final_skill.md", "report.json", "artifacts.zip"]
    if (job_dir / "initial_skill.md").exists():
        report["artifacts"].insert(0, "initial_skill.md")
    write(job_dir / "report.json", report)
    # Export only reviewed platform artifacts, never worker configuration or provider credentials.
    with zipfile.ZipFile(job_dir / "artifacts.zip", "w", zipfile.ZIP_DEFLATED) as bundle:
        for name in report["artifacts"]:
            path = job_dir / name
            if name != "artifacts.zip" and path.is_file():
                bundle.write(path, name)
    event(progress=100, model_calls=calls, phase="完成")


if __name__ == "__main__":
    payload = json.loads(sys.stdin.readline())
    try:
        main(payload)
    except Exception as exc:
        # Avoid tracebacks or HTTP response bodies containing provider credentials.
        message = str(exc)
        for model in payload["models"].values():
            for endpoint in model["endpoints"]:
                message = message.replace(endpoint["api_key"], "[密钥已隐藏]")
        message = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "[密钥已隐藏]", message)
        (Path(payload["job_dir"]) / "error.txt").write_text(message[:1200], encoding="utf-8")
        print("任务失败：" + message[:1200], flush=True)
        sys.exit(1)
