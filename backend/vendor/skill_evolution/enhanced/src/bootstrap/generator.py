"""Generate an initial SKILL package from a requirement document."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..model_client import ModelClient


@dataclass
class BootstrapConfig:
    """Configuration for bootstrap generation."""

    requirement: str
    output_dir: str
    skill_name: str = ""
    tool_contract: str = ""
    model_name: str = ""
    api_base: str = ""
    mock: bool = False
    overwrite: bool = False
    include_python_tool: bool = True


@dataclass
class BootstrapResult:
    """Artifacts produced by bootstrap generation."""

    skill_dir: str
    skill_name: str
    files: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    manifest_path: str = ""


class BootstrapSkillGenerator:
    """Create a standard skill package without depending on hand-written SKILL.md."""

    def __init__(self, model_client: Optional[ModelClient] = None):
        self.model_client = model_client

    def generate(self, config: BootstrapConfig) -> BootstrapResult:
        requirement = _read_requirement(config.requirement)
        if not requirement.strip():
            raise ValueError("requirement 内容为空，无法生成 SKILL")
        tool_contract = _read_optional_text(config.tool_contract)
        parse_warnings: List[str] = []

        if config.mock or not self.model_client:
            package = self._mock_package(
                requirement=requirement,
                skill_name=config.skill_name,
                tool_contract=tool_contract,
            )
            raw_model_output = ""
        else:
            raw_model_output = self._call_model(
                requirement=requirement,
                skill_name=config.skill_name,
                tool_contract=tool_contract,
            )
            try:
                package = _json_from_text(raw_model_output)
            except ValueError as exc:
                self._write_failed_model_output(
                    output_dir=Path(config.output_dir),
                    skill_name=config.skill_name,
                    raw_model_output=raw_model_output,
                    error=exc,
                    model_name=config.model_name,
                    api_base=config.api_base,
                )
                try:
                    repaired_output = self._repair_model_output(
                        raw_model_output=raw_model_output,
                        requirement=requirement,
                        skill_name=config.skill_name,
                        tool_contract=tool_contract,
                    )
                    package = _json_from_text(repaired_output)
                    raw_model_output = raw_model_output + "\n\n===== REPAIRED_JSON =====\n\n" + repaired_output
                    parse_warnings.append("模型首轮输出不是合法 JSON，已通过修复轮转换为合法 bootstrap 包。")
                except ValueError as repair_exc:
                    self._write_failed_model_output(
                        output_dir=Path(config.output_dir),
                        skill_name=config.skill_name,
                        raw_model_output=repaired_output if "repaired_output" in locals() else "",
                        error=repair_exc,
                        model_name=config.model_name,
                        api_base=config.api_base,
                        label="repair",
                    )
                    package = self._mock_package(
                        requirement=requirement,
                        skill_name=config.skill_name,
                        tool_contract=tool_contract,
                    )
                    raw_model_output = raw_model_output + (
                        "\n\n===== REPAIR_FAILED_FALLBACK_USED =====\n\n"
                        + str(repair_exc)
                    )
                    parse_warnings.append(
                        "模型输出及修复轮仍无法解析，已回退为按需求和固定工具契约生成的确定性初版包。"
                    )

        normalized = self._normalize_package(
            package=package,
            requirement=requirement,
            fallback_name=config.skill_name,
            tool_contract=tool_contract,
        )
        skill_name = _safe_name(str(normalized.get("skill_name") or config.skill_name or "bootstrap-skill"))
        skill_dir = Path(config.output_dir) / skill_name
        if skill_dir.exists():
            if not config.overwrite:
                raise FileExistsError(f"输出目录已存在，请使用 --overwrite 覆盖: {skill_dir}")
            shutil.rmtree(skill_dir)
        skill_dir.mkdir(parents=True, exist_ok=True)

        files: List[str] = []
        warnings: List[str] = []
        self._write_skill_md(skill_dir=skill_dir, package=normalized)
        files.append("SKILL.md")
        self._write_skill_config(skill_dir=skill_dir, package=normalized)
        files.append("skill_config.json")
        self._write_references(skill_dir=skill_dir, package=normalized, files=files)
        self._write_model_files(skill_dir=skill_dir, package=normalized, files=files)
        if config.include_python_tool:
            files.extend(self._write_python_tool(skill_dir=skill_dir, package=normalized))

        warnings.extend(parse_warnings)
        warnings.extend(self._validate(skill_dir))
        manifest = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "skill_name": skill_name,
            "skill_dir": str(skill_dir),
            "requirement_chars": len(requirement),
            "tool_contract_chars": len(tool_contract),
            "tool_contract_saved": bool(tool_contract),
            "model": {
                "used_model": bool(self.model_client and not config.mock),
                "model_name": config.model_name,
                "api_base": config.api_base,
                "api_key_saved": False,
            },
            "files": files,
            "warnings": warnings,
            "raw_model_output_saved": bool(raw_model_output),
        }
        manifest_path = skill_dir / "bootstrap_manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        files.append("bootstrap_manifest.json")

        if raw_model_output:
            raw_path = skill_dir / "bootstrap_model_output.txt"
            raw_path.write_text(raw_model_output, encoding="utf-8")
            files.append("bootstrap_model_output.txt")

        return BootstrapResult(
            skill_dir=str(skill_dir),
            skill_name=skill_name,
            files=files,
            warnings=warnings,
            manifest_path=str(manifest_path),
        )

    def _call_model(self, *, requirement: str, skill_name: str, tool_contract: str = "") -> str:
        system_prompt = """你是 SKILL 架构生成器。请根据需求从 0 生成一个标准 SKILL 包设计。

只输出 JSON，不要输出 Markdown 代码围栏之外的解释。必须包含:
{
  "skill_name": "kebab-case 名称",
  "description": "一句话触发描述",
  "skill_md": "完整 SKILL.md，必须含 YAML frontmatter，必须描述触发条件、输入、工具调用、输出和审计要求",
  "skill_config": {"name": "...", "description": "...", "tools": []},
  "references": {
    "requirements_summary.md": "...",
    "action_schema.json": {}
  },
  "tool_contract": {
    "entrypoint": "scripts/python/run_skill.py",
    "input_argument": "--input-json",
    "output": "JSON"
  },
  "files": {}
}

约束:
- SKILL.md 必须简洁，但足够指导 Hermes/Codex 执行。
- 如果提供了固定工具契约，必须把该契约作为硬约束，生成的 SKILL.md、action_schema.json 和工具代码都必须严格遵守其中的入口参数、输入 schema、输出 schema、动作类型、状态字段和审计字段；不要另起一套不兼容字段。
- 不要在 JSON 中输出冗长 Python 代码；`files` 可以为空对象。bootstrap 生成器会根据固定工具契约落地默认工具 adapter，避免大代码导致 JSON 截断。
- 未提供固定工具契约时，工具入口默认接受 --input-json，输出完整 JSON 到 stdout，失败时输出 JSON 到 stderr 并返回非 0。
- 工具结果必须包含契约要求的字段；若无契约，建议包含 success、audit_id、event_id、member_id、member_name、decision、actions、warnings、input_summary、audit_log_path。
- 工具必须在 skill_dir/logs 写入 JSONL 审计日志，便于人工定位。
- SKILL.md 必须定义工具契约和最终回答必须附带工具 JSON。
- 不要包含 API key、密钥、真实个人隐私。
- 只生成初版，后续会由 self-evolve 验证和优化。"""
        user_prompt = f"""## 期望 skill_name
{skill_name or "(请自动命名)"}

## 需求
{requirement}

## 固定工具契约（如有，必须严格遵守）
{tool_contract or "(未提供固定工具契约，请根据需求自行设计并保持 SKILL.md、schema、工具代码一致)"}
"""
        response = self.model_client.generate_with_system(
            system_prompt,
            user_prompt,
            temperature=0.2,
            max_tokens=5000,
        )
        if not response.strip():
            raise ValueError("模型未返回 bootstrap 内容")
        return response

    def _repair_model_output(
        self,
        *,
        raw_model_output: str,
        requirement: str,
        skill_name: str,
        tool_contract: str = "",
    ) -> str:
        """Ask the model to convert a non-JSON bootstrap draft into valid JSON."""
        system_prompt = """你是严格的 JSON 修复器。把用户提供的 bootstrap 草稿转换成一个合法 JSON object。

只输出 JSON，不要输出解释、Markdown 或代码围栏。必须包含:
- skill_name
- description
- skill_md
- skill_config
- references
- tool_contract
- files

如果草稿中已有 SKILL.md、Python 代码或 schema，保留其语义并修复 JSON 转义问题。不要添加 API key、密钥或真实隐私。"""
        user_prompt = f"""## 期望 skill_name
{skill_name or "(请从草稿或需求推断)"}

## 原始需求
{requirement}

## 固定工具契约
{tool_contract or "(未提供)"}

## 需要转换为合法 JSON 的 bootstrap 草稿
{raw_model_output}
"""
        response = self.model_client.generate_with_system(
            system_prompt,
            user_prompt,
            temperature=0.0,
            max_tokens=9000,
        )
        if not response.strip():
            raise ValueError("模型修复 bootstrap JSON 失败：空响应")
        return response

    def _write_failed_model_output(
        self,
        *,
        output_dir: Path,
        skill_name: str,
        raw_model_output: str,
        error: Exception,
        model_name: str,
        api_base: str,
        label: str = "initial",
    ) -> None:
        """Persist failed model output for audit without saving secrets."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = _safe_name(skill_name or "bootstrap")
        failure_dir = output_dir / f"_failed_bootstrap_{safe_name}_{label}_{timestamp}"
        failure_dir.mkdir(parents=True, exist_ok=True)
        (failure_dir / "bootstrap_model_output.txt").write_text(raw_model_output, encoding="utf-8")
        metadata = {
            "failed_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"{type(error).__name__}: {error}",
            "model_name": model_name,
            "api_base": api_base,
            "api_key_saved": False,
            "raw_model_output_chars": len(raw_model_output),
        }
        (failure_dir / "bootstrap_error.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _mock_package(self, *, requirement: str, skill_name: str, tool_contract: str = "") -> Dict[str, Any]:
        name = _safe_name(skill_name or _guess_name(requirement))
        title = name.replace("-", " ").title()
        contract_section = ""
        if tool_contract:
            contract_section = f"""

## 固定工具契约

本技能必须遵守 `reference/tool_contract.md` 中定义的工具入口、输入参数、输出 JSON schema、动作类型和审计要求。
"""
        skill_md = f"""---
name: {name}
description: 根据需求自动生成的初版技能。用于处理与 {title} 相关的任务，调用本技能工具产出可审计 JSON，并根据执行结果回答。
---

# {title}

这是由 bootstrap 流程根据需求自动生成的初版 SKILL。后续应通过 self-evolve 使用黑盒数据集验证并优化。

## 适用场景

当用户请求与以下需求相关时使用本技能：

```text
{requirement[:1200]}
```

## 输入处理

- 优先从用户请求中提取结构化 JSON。
- 如果输入不是 JSON，需要整理为字段清晰的 JSON，再调用工具。
- 不要编造缺失的关键事实；缺失时在工具输入中显式标记为 `unknown` 或空值。

## 必须调用工具

调用本技能内置工具：

```bash
cd <skill_root> && python3 scripts/python/run_skill.py --input-json '<结构化输入JSON>'
```
{contract_section}

## 输出要求

- 先简要说明识别到的任务、是否成功以及关键动作。
- 如发生降级、跳过或失败，说明原因。
- 最终回答末尾必须附带完整工具 JSON，使用 `工具结果JSON:` 前缀，方便审计和自动评分。
"""
        return {
            "skill_name": name,
            "description": f"根据需求自动生成的初版技能: {title}",
            "skill_md": skill_md,
            "skill_config": {
                "name": name,
                "description": f"Bootstrap generated skill for {title}",
                "tools": ["scripts/python/run_skill.py"],
            },
            "references": {
                "requirements_summary.md": f"# 需求摘要\n\n{requirement}\n",
                "action_schema.json": _fixed_action_schema() if tool_contract else {
                    "type": "object",
                    "required": ["success", "decision", "actions", "warnings"],
                    "properties": {
                        "success": {"type": "boolean"},
                        "decision": {"type": "object"},
                        "actions": {"type": "array"},
                        "warnings": {"type": "array"},
                    },
                },
                **({"tool_contract.md": tool_contract} if tool_contract else {}),
            },
            "tool_contract": {
                "entrypoint": "scripts/python/run_skill.py",
                "input_argument": "--input-json",
                "output": "JSON",
            },
        }

    def _normalize_package(
        self,
        *,
        package: Dict[str, Any],
        requirement: str,
        fallback_name: str,
        tool_contract: str = "",
    ) -> Dict[str, Any]:
        name = _safe_name(str(package.get("skill_name") or fallback_name or _guess_name(requirement)))
        description = str(package.get("description") or f"Bootstrap generated skill: {name}")
        skill_md = str(package.get("skill_md") or "").strip()
        if not skill_md:
            skill_md = self._mock_package(
                requirement=requirement,
                skill_name=name,
                tool_contract=tool_contract,
            )["skill_md"]
        if not skill_md.startswith("---"):
            skill_md = f"---\nname: {name}\ndescription: {description}\n---\n\n{skill_md}"
        skill_config = package.get("skill_config") if isinstance(package.get("skill_config"), dict) else {}
        skill_config.setdefault("name", name)
        skill_config.setdefault("description", description)
        references = package.get("references") if isinstance(package.get("references"), dict) else {}
        if "requirements_summary.md" not in references:
            references["requirements_summary.md"] = f"# 需求摘要\n\n{requirement}\n"
        if tool_contract and "tool_contract.md" not in references:
            references["tool_contract.md"] = tool_contract
        if tool_contract:
            references["action_schema.json"] = _fixed_action_schema()
        return {
            "skill_name": name,
            "description": description,
            "skill_md": _ensure_frontmatter_description(skill_md, name=name, description=description),
            "skill_config": skill_config,
            "references": references,
            "tool_contract": package.get("tool_contract") if isinstance(package.get("tool_contract"), dict) else {},
            "files": package.get("files") if isinstance(package.get("files"), dict) else {},
        }

    def _write_skill_md(self, *, skill_dir: Path, package: Dict[str, Any]) -> None:
        (skill_dir / "SKILL.md").write_text(str(package["skill_md"]).rstrip() + "\n", encoding="utf-8")

    def _write_skill_config(self, *, skill_dir: Path, package: Dict[str, Any]) -> None:
        (skill_dir / "skill_config.json").write_text(
            json.dumps(package["skill_config"], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _write_references(self, *, skill_dir: Path, package: Dict[str, Any], files: List[str]) -> None:
        reference_dir = skill_dir / "reference"
        reference_dir.mkdir(parents=True, exist_ok=True)
        references = package.get("references") or {}
        for raw_name, content in references.items():
            name = _safe_relative_file_name(str(raw_name))
            if not name:
                continue
            path = reference_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, (dict, list)):
                path.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            else:
                path.write_text(str(content).rstrip() + "\n", encoding="utf-8")
            files.append(str(path.relative_to(skill_dir)))

    def _write_model_files(self, *, skill_dir: Path, package: Dict[str, Any], files: List[str]) -> None:
        for raw_name, content in (package.get("files") or {}).items():
            name = _safe_relative_file_name(str(raw_name))
            if not name or name in {"SKILL.md", "skill_config.json", "bootstrap_manifest.json"}:
                continue
            path = skill_dir / name
            if not _is_relative_to(path.resolve(), skill_dir.resolve()):
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, (dict, list)):
                path.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            else:
                path.write_text(str(content).rstrip() + "\n", encoding="utf-8")
            relative = str(path.relative_to(skill_dir))
            if relative not in files:
                files.append(relative)

    def _write_python_tool(self, *, skill_dir: Path, package: Dict[str, Any]) -> List[str]:
        script_dir = skill_dir / "scripts" / "python"
        script_dir.mkdir(parents=True, exist_ok=True)
        written: List[str] = []
        run_skill = script_dir / "run_skill.py"
        mock_tools = script_dir / "mock_tools.py"
        has_fixed_contract = bool((package.get("references") or {}).get("tool_contract.md"))
        if has_fixed_contract:
            run_skill.write_text(_CONTRACT_RUN_SKILL_TEMPLATE, encoding="utf-8")
            mock_tools.write_text(_CONTRACT_MOCK_TOOLS_TEMPLATE, encoding="utf-8")
            return ["scripts/python/run_skill.py", "scripts/python/mock_tools.py"]
        if not run_skill.exists():
            run_skill.write_text(_RUN_SKILL_TEMPLATE, encoding="utf-8")
            written.append("scripts/python/run_skill.py")
        if not mock_tools.exists():
            mock_tools.write_text(_MOCK_TOOLS_TEMPLATE, encoding="utf-8")
            written.append("scripts/python/mock_tools.py")
        return written

    def _validate(self, skill_dir: Path) -> List[str]:
        warnings: List[str] = []
        skill_md = skill_dir / "SKILL.md"
        text = skill_md.read_text(encoding="utf-8")
        if not text.startswith("---"):
            warnings.append("SKILL.md 缺少 YAML frontmatter")
        if "description:" not in text[:500]:
            warnings.append("SKILL.md frontmatter 缺少 description")
        if "工具结果JSON" not in text:
            warnings.append("SKILL.md 未显式要求输出 工具结果JSON")
        if not (skill_dir / "scripts" / "python" / "run_skill.py").exists():
            warnings.append("未生成默认工具入口 scripts/python/run_skill.py")
        contract = skill_dir / "reference" / "tool_contract.md"
        run_skill = skill_dir / "scripts" / "python" / "run_skill.py"
        if contract.exists() and run_skill.exists():
            tool_text = run_skill.read_text(encoding="utf-8", errors="replace")
            helper = run_skill.parent / "mock_tools.py"
            if helper.exists():
                tool_text += "\n" + helper.read_text(encoding="utf-8", errors="replace")
            for required in ("should_welcome", "screen_card", "playback", "restore_environment", "family_group_notify", "safe_log_only"):
                if required not in tool_text:
                    warnings.append(f"固定契约工具入口缺少关键字段/动作: {required}")
        return warnings


def _read_requirement(value: str) -> str:
    path = Path(value)
    if path.exists() and path.is_file():
        return path.read_text(encoding="utf-8")
    return value


def _read_optional_text(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    path = Path(value)
    if path.exists() and path.is_file():
        return path.read_text(encoding="utf-8")
    return value


def _json_from_text(text: str) -> Dict[str, Any]:
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = []
    if match:
        candidates.append(match.group(1))
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last > first:
        candidates.append(text[first:last + 1])
    candidates.append(text)
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("无法从模型输出中解析 JSON")


def _safe_name(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9_.-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-.")
    return value or "bootstrap-skill"


def _guess_name(requirement: str) -> str:
    text = requirement.lower()
    if "iot" in text or "家庭" in requirement or "欢迎" in requirement:
        return "home-member-welcome"
    if "ticket" in text or "火车" in requirement:
        return "train-ticket-search"
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9]+", requirement)
    return "-".join(words[:4]).lower() if words else "bootstrap-skill"


def _safe_relative_file_name(value: str) -> str:
    value = value.replace("\\", "/").lstrip("/")
    parts = []
    for part in value.split("/"):
        if not part or part in {".", ".."}:
            continue
        clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", part)
        if clean:
            parts.append(clean)
    return "/".join(parts)


def _ensure_frontmatter_description(skill_md: str, *, name: str, description: str) -> str:
    text = skill_md.strip()
    if not text.startswith("---"):
        return f"---\nname: {name}\ndescription: {description}\n---\n\n{text}"
    end = text.find("\n---", 3)
    if end == -1:
        return f"---\nname: {name}\ndescription: {description}\n---\n\n{text}"
    frontmatter = text[3:end]
    body = text[end:]
    lines = frontmatter.splitlines()
    has_name = any(line.strip().startswith("name:") for line in lines)
    has_description = any(line.strip().startswith("description:") for line in lines)
    additions = []
    if not has_name:
        additions.append(f"name: {name}")
    if not has_description:
        additions.append(f"description: {description}")
    if additions:
        frontmatter = "\n".join(additions + lines)
    return f"---{frontmatter}{body}"


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _fixed_action_schema() -> Dict[str, Any]:
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "HomeMemberWelcomeOutput",
        "type": "object",
        "required": [
            "success",
            "audit_id",
            "event_id",
            "member_id",
            "member_name",
            "decision",
            "actions",
            "warnings",
            "input_summary",
            "audit_log_path",
        ],
        "properties": {
            "success": {"type": "boolean"},
            "audit_id": {"type": "string"},
            "event_id": {"type": "string"},
            "member_id": {"type": "string"},
            "member_name": {"type": "string"},
            "decision": {
                "type": "object",
                "required": ["should_welcome", "reason", "confidence", "policy_id"],
                "properties": {
                    "should_welcome": {"type": "boolean"},
                    "reason": {"type": "string"},
                    "confidence": {"type": "number"},
                    "policy_id": {"type": "string"},
                },
            },
            "actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["type", "status", "payload"],
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": [
                                "screen_card",
                                "playback",
                                "restore_environment",
                                "family_group_notify",
                                "safe_log_only",
                            ],
                        },
                        "status": {"type": "string", "enum": ["success", "skipped", "failed"]},
                        "payload": {"type": "object"},
                    },
                },
            },
            "warnings": {"type": "array", "items": {"type": "string"}},
            "input_summary": {
                "type": "object",
                "required": ["normalization_trace"],
                "properties": {"normalization_trace": {"type": "array", "items": {"type": "string"}}},
            },
            "audit_log_path": {"type": "string"},
        },
    }


_RUN_SKILL_TEMPLATE = '''#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from mock_tools import execute_actions


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run bootstrap generated skill tool.")
    parser.add_argument("--input-json", required=True, help="结构化输入 JSON")
    parser.add_argument("--pretty", action="store_true", help="格式化输出 JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        payload = json.loads(args.input_json)
        result = execute_actions(payload, skill_dir=Path(__file__).resolve().parents[2])
        print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
        return 0
    except Exception as exc:
        error = {
            "success": False,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
'''


_MOCK_TOOLS_TEMPLATE = '''from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict
import json


def execute_actions(payload: Dict[str, Any], *, skill_dir: Path) -> Dict[str, Any]:
    logs_dir = skill_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "success": True,
        "decision": {
            "accepted": True,
            "reason": "bootstrap 默认工具已接收结构化输入",
        },
        "actions": [
            {
                "type": "audit_log",
                "status": "success",
                "payload": {
                    "input_keys": sorted(payload.keys()),
                },
            }
        ],
        "warnings": [],
        "input": payload,
    }
    audit_path = logs_dir / "bootstrap_tool_audit.jsonl"
    record = {
        "logged_at": datetime.now().isoformat(timespec="seconds"),
        "decision": result["decision"],
        "actions": [a["type"] for a in result["actions"]],
    }
    with audit_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\\n")
    result["audit_log_path"] = str(audit_path)
    return result
'''


_CONTRACT_RUN_SKILL_TEMPLATE = '''#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from mock_tools import execute_actions


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run fixed-contract home member welcome skill tool.")
    parser.add_argument("--input-json", required=True, help="融合感知事件 JSON")
    parser.add_argument("--pretty", action="store_true", help="格式化输出 JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        payload = json.loads(args.input_json)
        if not isinstance(payload, dict):
            raise ValueError("input JSON must be an object")
        result = execute_actions(payload, skill_dir=Path(__file__).resolve().parents[2])
        print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
        return 0
    except Exception as exc:
        error = {
            "success": False,
            "decision": {"should_welcome": False, "reason": str(exc), "confidence": 0.0, "policy_id": "home_member_welcome_v1"},
            "actions": [{"type": "safe_log_only", "status": "success", "payload": {"reason": "tool_error"}}],
            "warnings": ["tool_error:" + type(exc).__name__],
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
'''


_CONTRACT_MOCK_TOOLS_TEMPLATE = '''from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple
import hashlib
import json


CONFIDENCE_THRESHOLD = 0.80
INVALID_MEMBER_IDS = {"", "unknown", "visitor", "guest", "stranger", "none", "null"}
INVALID_MEMBER_ROLES = {"visitor", "guest", "unknown", "stranger", "courier"}
FULL_ACTIONS = ["screen_card", "playback", "restore_environment", "family_group_notify"]


def execute_actions(payload: Dict[str, Any], *, skill_dir: Path) -> Dict[str, Any]:
    normalized, trace = normalize_event(payload)
    decision, warnings = decide(normalized)
    actions = build_actions(decision, normalized)
    result = {
        "success": True,
        "audit_id": normalized["audit_id"],
        "event_id": normalized["event_id"],
        "member_id": normalized["member_id"],
        "member_name": normalized["member_name"],
        "decision": decision,
        "actions": actions,
        "warnings": warnings,
        "input_summary": {
            "normalization_trace": trace,
            "event_type": normalized["event_type"],
            "member_role": normalized["member_role"],
            "fusion_confirmed": normalized["fusion_confirmed"],
            "household_member": normalized["household_member"],
        },
        "audit_log_path": "",
    }
    result["audit_log_path"] = write_audit_log(skill_dir, result)
    return result


def normalize_event(raw: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    trace: List[str] = []
    member = raw.get("member") if isinstance(raw.get("member"), dict) else {}
    subject = raw.get("subject") if isinstance(raw.get("subject"), dict) else {}
    context = raw.get("context") if isinstance(raw.get("context"), dict) else None
    if context is None:
        context = raw.get("scene") if isinstance(raw.get("scene"), dict) else {}
        if isinstance(raw.get("scene"), dict):
            trace.append("scene->context")
    fusion_evidence = raw.get("fusion_evidence")
    if fusion_evidence is None:
        fusion_evidence = raw.get("signals")
        if fusion_evidence is not None:
            trace.append("signals->fusion_evidence")
    if fusion_evidence is None:
        fusion_evidence = {}

    if subject:
        trace.append("subject->member")
    if not member and (raw.get("member_id") or raw.get("member_name") or raw.get("confidence") is not None):
        trace.append("flat_member_fields->member")
    if any(key in raw for key in ("face_matched", "voice_matched", "wifi_connected", "fttr_entry_detected")):
        trace.append("flat_signal_fields->fusion_evidence")

    for key in ("arrival_type", "weather", "quiet_hours"):
        if key in raw and key not in context:
            context[key] = raw.get(key)
            trace.append(f"flat_{key}->context.{key}")

    event_type = first_present(raw.get("event_type"), raw.get("event_kind"), raw.get("arrival_type"))
    if raw.get("event_kind") is not None and raw.get("event_type") is None:
        trace.append("event_kind->event_type")
    event_id = string_value(raw.get("event_id"))
    audit_id = string_value(raw.get("audit_id")) or event_id or stable_id(raw)
    if not raw.get("audit_id"):
        trace.append("audit_id_fallback")

    member_id = string_value(first_present(
        raw.get("member_id"),
        member.get("member_id"),
        member.get("id"),
        subject.get("id"),
    ))
    member_name = string_value(first_present(
        raw.get("member_name"),
        member.get("member_name"),
        member.get("name"),
        subject.get("display_name"),
        subject.get("name"),
    ))
    confidence = as_float(first_present(
        raw.get("confidence"),
        member.get("confidence"),
        subject.get("confidence_score"),
        subject.get("confidence"),
    ))
    member_role = string_value(first_present(
        raw.get("member_role"),
        member.get("role"),
        subject.get("relationship"),
    )).lower()

    fusion_result = raw.get("fusion_result") if isinstance(raw.get("fusion_result"), dict) else {}
    fusion_confirmed = True
    if fusion_result.get("confirmed") is False:
        fusion_confirmed = False
    household_member = raw.get("household_member")
    household_member_explicit = household_member is not None
    if household_member is None:
        household_member = infer_household_member(member_id, member_role)

    return {
        "event_type": string_value(event_type),
        "event_id": event_id,
        "audit_id": audit_id,
        "event_status": string_value(raw.get("event_status")).lower(),
        "cancelled": as_bool(raw.get("cancelled")),
        "dedupe": raw.get("dedupe") if isinstance(raw.get("dedupe"), dict) else {},
        "fusion_confirmed": fusion_confirmed,
        "household_member": as_bool(household_member) if household_member is not None else infer_household_member(member_id, member_role),
        "household_member_explicit": household_member_explicit,
        "member_id": member_id,
        "member_name": member_name,
        "member_role": member_role,
        "confidence": confidence,
        "fusion_evidence": fusion_evidence,
        "context": context,
    }, trace


def decide(event: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    warnings: List[str] = []
    confidence = event["confidence"]
    reason = ""
    should_welcome = True

    if not is_arrival_event(event["event_type"], event["context"]):
        should_welcome = False
        reason = "事件不是 member_arrival 到家事件"
        warnings.append("non_arrival_event")
    elif event["event_status"] == "cancelled" or event["cancelled"]:
        should_welcome = False
        reason = "事件已取消，不能触发欢迎"
        warnings.append("event_cancelled")
    elif event["dedupe"].get("is_duplicate") is True:
        should_welcome = False
        reason = "重复到家事件，避免重复执行欢迎"
        warnings.append("duplicate_event")
    elif not event["fusion_confirmed"]:
        should_welcome = False
        reason = "融合结果未确认，安全降级"
        warnings.append("fusion_not_confirmed")
    elif event["household_member_explicit"] and event["household_member"] is False:
        should_welcome = False
        reason = "不是家庭成员，安全降级"
        warnings.append("not_household_member")
    elif event["member_id"].lower() in INVALID_MEMBER_IDS or event["member_role"] in INVALID_MEMBER_ROLES:
        should_welcome = False
        reason = "未知成员或访客，不触发完整欢迎"
        warnings.append("member_not_found:" + (event["member_id"] or "unknown"))
    elif confidence is None:
        should_welcome = False
        reason = "置信度缺失，安全降级"
        warnings.append("missing_confidence")
    elif confidence < CONFIDENCE_THRESHOLD:
        should_welcome = False
        reason = f"低置信度 {confidence:.2f}，低于阈值 {CONFIDENCE_THRESHOLD:.2f}"
        warnings.append(f"low_confidence:{confidence:.2f}")
    else:
        reason = "高置信度家庭成员到家，触发个性化欢迎"

    return {
        "should_welcome": should_welcome,
        "reason": reason,
        "confidence": float(confidence or 0.0),
        "policy_id": policy_id_for(event, should_welcome),
    }, warnings


def build_actions(decision: Dict[str, Any], event: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not decision["should_welcome"]:
        return [{"type": "safe_log_only", "status": "success", "payload": {"reason": decision["reason"]}}]
    context = event["context"] if isinstance(event["context"], dict) else {}
    quiet = as_bool(context.get("quiet_hours"))
    playback_mode = "silent_summary" if quiet else "voice"
    return [
        {
            "type": "screen_card",
            "status": "success",
            "payload": {
                "member_id": event["member_id"],
                "member_name": event["member_name"],
                "card": "personal_welcome",
            },
        },
        {
            "type": "playback",
            "status": "success",
            "payload": {
                "mode": playback_mode,
                "content": ["todo", "family_messages"],
            },
        },
        {
            "type": "restore_environment",
            "status": "success",
            "payload": {
                "profile": event["member_id"] or "default",
                "targets": ["lighting", "temperature", "curtain"],
            },
        },
        {
            "type": "family_group_notify",
            "status": "success",
            "payload": {
                "message": f"{event['member_name'] or event['member_id']} 已到家",
            },
        },
    ]


def write_audit_log(skill_dir: Path, result: Dict[str, Any]) -> str:
    logs_dir = skill_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / "home_member_welcome_audit.jsonl"
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "audit_id": result["audit_id"],
        "event_id": result["event_id"],
        "member_id": result["member_id"],
        "decision": {"should_welcome": result["decision"]["should_welcome"]},
        "actions": [{"type": action.get("type")} for action in result["actions"]],
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\\n")
    return str(path)


def first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def string_value(value: Any) -> str:
    return "" if value is None else str(value)


def as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def infer_household_member(member_id: str, member_role: str) -> bool:
    if member_id.lower() in INVALID_MEMBER_IDS or member_role in INVALID_MEMBER_ROLES:
        return False
    return bool(member_id)


def is_arrival_event(event_type: str, context: Dict[str, Any]) -> bool:
    text = event_type.lower()
    arrival_type = string_value(context.get("arrival_type") if isinstance(context, dict) else "").lower()
    if any(token in text for token in ("member_arrival", "arrival_detected", "home_arrival", "arrive", "到家")):
        return True
    if text in {"arrival", "home"}:
        return True
    if "location_ping" in text or "departure" in text or "heartbeat" in text:
        return False
    return "arrival" in arrival_type and "departure" not in arrival_type


def policy_id_for(event: Dict[str, Any], should_welcome: bool) -> str:
    if not should_welcome:
        return "home_member_welcome_safe_degrade"
    role = event.get("member_role") or event.get("member_id") or "default"
    arrival = ""
    context = event.get("context")
    if isinstance(context, dict):
        arrival = string_value(context.get("arrival_type"))
    suffix = "_".join(part for part in (role, arrival) if part)
    return "home_member_welcome_" + (suffix or "default")


def stable_id(raw: Dict[str, Any]) -> str:
    text = json.dumps(raw, ensure_ascii=False, sort_keys=True)
    return "audit_" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
'''
