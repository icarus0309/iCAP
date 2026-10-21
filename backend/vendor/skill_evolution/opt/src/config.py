"""
OPT 优化器配置文件
"""
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


_DOTENV_LOADED = False


def _parse_env_line(line: str) -> Optional[Tuple[str, str]]:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export "):].strip()
    if "=" not in line:
        return None
    key, value = line.split("=", 1)
    key = key.strip()
    value = value.strip()
    if not key:
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return key, value


def load_dotenv(overwrite: bool = False) -> None:
    """Load simple KEY=VALUE entries from .env without adding dependencies.

    Search order keeps the caller's working directory first, then the project
    root. Existing process environment values win unless overwrite=True.
    """
    global _DOTENV_LOADED
    if _DOTENV_LOADED and not overwrite:
        return

    project_root = Path(__file__).resolve().parents[1]
    candidates = [
        Path.cwd() / ".env",
        Path.cwd() / "SkillTuning" / ".env",
        project_root / ".env",
    ]
    seen = set()
    for path in candidates:
        path = path.resolve()
        if path in seen or not path.exists():
            continue
        seen.add(path)
        try:
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                parsed = _parse_env_line(raw_line)
                if not parsed:
                    continue
                key, value = parsed
                if overwrite or key not in os.environ:
                    os.environ[key] = value
        except OSError:
            continue
    _DOTENV_LOADED = True


@dataclass
class ModelConfig:
    """模型配置"""
    model_name: str = field(default_factory=lambda: os.getenv("MODEL_NAME", "mimo-v2-flash"))
    api_key: Optional[str] = field(default_factory=lambda: os.getenv("API_KEY"))
    api_base: Optional[str] = field(default_factory=lambda: os.getenv("API_BASE"))
    temperature: float = field(default_factory=lambda: float(os.getenv("TEMPERATURE", "0.7")))
    max_tokens: int = field(default_factory=lambda: int(os.getenv("MAX_TOKENS", "1000")))
    timeout: int = field(default_factory=lambda: int(os.getenv("TIMEOUT", "60")))


def load_model_config() -> ModelConfig:
    """从环境变量加载运行模型配置"""
    load_dotenv()
    return ModelConfig(
        model_name=os.getenv("MODEL_NAME", "mimo-v2-flash"),
        api_key=os.getenv("API_KEY"),
        api_base=os.getenv("API_BASE"),
        temperature=float(os.getenv("TEMPERATURE", "0.7")),
        max_tokens=int(os.getenv("MAX_TOKENS", "1000")),
        timeout=int(os.getenv("TIMEOUT", "60")),
    )


def load_eval_model_config() -> ModelConfig:
    """从环境变量加载评估模型配置（默认复用运行模型）"""
    load_dotenv()
    eval_model_name = os.getenv("EVAL_MODEL")
    if eval_model_name:
        return ModelConfig(
            model_name=eval_model_name,
            api_key=os.getenv("EVAL_API_KEY", os.getenv("API_KEY")),
            api_base=os.getenv("EVAL_API_BASE", os.getenv("API_BASE")),
            temperature=float(os.getenv("EVAL_TEMPERATURE", "0.3")),
            max_tokens=int(os.getenv("EVAL_MAX_TOKENS", "2000")),
            timeout=int(os.getenv("EVAL_TIMEOUT", "120")),
        )
    return load_model_config()


def load_gen_model_config() -> ModelConfig:
    """从环境变量加载生成模型配置（默认复用运行模型）"""
    load_dotenv()
    gen_model_name = os.getenv("GEN_MODEL")
    if gen_model_name:
        return ModelConfig(
            model_name=gen_model_name,
            api_key=os.getenv("GEN_API_KEY", os.getenv("API_KEY")),
            api_base=os.getenv("GEN_API_BASE", os.getenv("API_BASE")),
            temperature=float(os.getenv("GEN_TEMPERATURE", "0.7")),
            max_tokens=int(os.getenv("GEN_MAX_TOKENS", "4000")),
            timeout=int(os.getenv("GEN_TIMEOUT", "120")),
        )
    return load_model_config()


def load_exec_model_config() -> ModelConfig:
    """从环境变量加载执行模型配置（direct 模式单次 LLM 调用用，默认复用运行模型）"""
    load_dotenv()
    exec_model_name = os.getenv("EXEC_MODEL")
    if exec_model_name:
        return ModelConfig(
            model_name=exec_model_name,
            api_key=os.getenv("EXEC_API_KEY", os.getenv("API_KEY")),
            api_base=os.getenv("EXEC_API_BASE", os.getenv("API_BASE")),
            temperature=float(os.getenv("EXEC_TEMPERATURE", "0.7")),
            max_tokens=int(os.getenv("EXEC_MAX_TOKENS", "4000")),
            timeout=int(os.getenv("EXEC_TIMEOUT", "120")),
        )
    return load_model_config()


def load_exec_mode() -> str:
    """执行模式：`hermes`（默认，Hermes 多轮 agent）或 `direct`（单次 LLM 调用）。"""
    load_dotenv()
    mode = os.getenv("SKILL_EXEC_MODE", "hermes").strip().lower()
    return mode if mode in ("hermes", "direct") else "hermes"


def load_scorer_weights():
    """从环境变量加载评分器权重（判断权重 + 方向权重）。

    权重默认值定义在 `ScorerWeights` 的 `default_factory` 中（schemas.py），
    此处只负责确保 `.env` 已加载，再返回一个按 env 取值的 `ScorerWeights`。
    """
    load_dotenv()
    from src.self_evolve.schemas import ScorerWeights

    return ScorerWeights()


def load_output_weights() -> Dict[str, float]:
    """从环境变量加载结果子类型权重（文本 / 工具 / 文件 / onlyllm / word / format）。

    返回 key：`text` / `tool` / `file` / `onlyllm` / `word` / `format`，与数据集 `output` 字段 type 一致。
    `onlyllm` 默认 0，仅在 direct 模式 + onlyllm 评估场景下由 env 置为 1。
    `word` 默认 0，仅在黑/白名单硬校验（格式校验）场景下由 env 置为 1。
    `format` 默认 0，仅在 JSON 结构格式校验场景下由 env 置为 1。
    """
    load_dotenv()
    return {
        "text": float(os.getenv("OUTPUT_WEIGHT_TEXT", "0.30")),
        "tool": float(os.getenv("OUTPUT_WEIGHT_TOOL", "0.60")),
        "file": float(os.getenv("OUTPUT_WEIGHT_FILE", "0.10")),
        "onlyllm": float(os.getenv("OUTPUT_WEIGHT_ONLYLLM", "0.0")),
        "word": float(os.getenv("OUTPUT_WEIGHT_WORD", "0.0")),
        "format": float(os.getenv("OUTPUT_WEIGHT_FORMAT", "0.0")),
    }


def load_output_format_example() -> Optional[list]:
    """从环境变量加载结果格式示例（JSON 数组，定义目标字段结构）。

    例如记忆场景：
    OUTPUT_FORMAT_EXAMPLE=[{"topic":"个人身份","subtopic":"姓名","details":"用户的名字是林月"}]

    返回解析后的 list（或 dict）；未配置 / 非 JSON / 解析失败时返回 None。
    """
    load_dotenv()
    raw = os.getenv("OUTPUT_FORMAT_EXAMPLE", "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(parsed, (list, dict)):
        return parsed
    return None
