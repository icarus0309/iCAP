"""
GEPA优化器配置文件
"""
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple


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




@dataclass
class GEPAConfig:
    """GEPA优化器配置"""
    iterations: int = 10
    pareto_size: int = 180
    minibatch_size: int = 10
    random_seed: Optional[int] = 42
    tie_breaker: str = "PREFER_CHILD"  # PREFER_ROOT, PREFER_CHILD, RANDOM
    max_metric_calls: Optional[int] = None
    timeout_seconds: Optional[int] = None
    no_improvement_patience: Optional[int] = None
    enable_merge: bool = True


def load_model_config() -> ModelConfig:
    """从环境变量加载模型配置（用于运行/生成）"""
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
    """从环境变量加载评估模型配置

    如果未设置评估模型相关环境变量，则使用运行模型配置
    """
    load_dotenv()
    eval_model_name = os.getenv("EVAL_MODEL")
    if eval_model_name:
        # 使用独立的评估模型配置
        return ModelConfig(
            model_name=eval_model_name,
            api_key=os.getenv("EVAL_API_KEY", os.getenv("API_KEY")),
            api_base=os.getenv("EVAL_API_BASE", os.getenv("API_BASE")),
            temperature=float(os.getenv("EVAL_TEMPERATURE", "0.3")),  # 评估通常用较低温度
            max_tokens=int(os.getenv("EVAL_MAX_TOKENS", "2000")),
            timeout=int(os.getenv("EVAL_TIMEOUT", "120")),
        )
    else:
        # 未配置评估模型，使用运行模型
        return load_model_config()


def load_gen_model_config() -> ModelConfig:
    """从环境变量加载生成模型配置（用于prompt变异/合并）

    如果未设置生成模型相关环境变量，则使用运行模型配置
    """
    load_dotenv()
    gen_model_name = os.getenv("GEN_MODEL")
    if gen_model_name:
        # 使用独立的生成模型配置
        return ModelConfig(
            model_name=gen_model_name,
            api_key=os.getenv("GEN_API_KEY", os.getenv("API_KEY")),
            api_base=os.getenv("GEN_API_BASE", os.getenv("API_BASE")),
            temperature=float(os.getenv("GEN_TEMPERATURE", "0.7")),  # 生成通常用较高温度
            max_tokens=int(os.getenv("GEN_MAX_TOKENS", "4000")),
            timeout=int(os.getenv("GEN_TIMEOUT", "120")),
        )
    else:
        # 未配置生成模型，使用运行模型
        return load_model_config()




def load_scorer_weights():
    """从环境变量加载评分器权重（判断权重 + 方向/门控权重）。

    权重默认值定义在 `ScorerWeights` 的 `default_factory` 中（schemas.py），
    此处只负责确保 `.env` 已加载，再返回一个按 env 取值的 `ScorerWeights`。
    """
    load_dotenv()
    from src.self_evolve.schemas import ScorerWeights

    return ScorerWeights()


def load_output_weights() -> Dict[str, float]:
    """从环境变量加载结果子类型权重。

    返回 key：`text` / `tool` / `file` / `onlyllm` / `word` / `format`，与数据集 `output` 字段 type 一致。
    `word`/`format` 默认 0，仅在关键词/格式确定性校验场景下由 env 置为 1。
    """
    load_dotenv()
    return {
        "text": float(os.getenv("OUTPUT_WEIGHT_TEXT", "0.30")),
        "tool": float(os.getenv("OUTPUT_WEIGHT_TOOL", "0.60")),
        "file": float(os.getenv("OUTPUT_WEIGHT_FILE", "0.10")),
        "onlyllm": float(os.getenv("OUTPUT_WEIGHT_ONLYLLM", "1")),
        "word": float(os.getenv("OUTPUT_WEIGHT_WORD", "0.0")),
        "format": float(os.getenv("OUTPUT_WEIGHT_FORMAT", "0.0")),
    }


def load_output_format_example():
    """加载格式校验示例（OUTPUT_FORMAT_EXAMPLE），每次强制重读 .env 以支持运行期修改。

    示例：
      OUTPUT_FORMAT_EXAMPLE=[{"topic":"个人身份","subtopic":"姓名","details":"用户的名字是林月"}]
    返回解析后的 list/dict，失败返回 None。
    """
    import json as _json
    load_dotenv(overwrite=True)
    raw = os.getenv("OUTPUT_FORMAT_EXAMPLE", "").strip()
    if not raw:
        return None
    try:
        example = _json.loads(raw)
    except (ValueError, TypeError):
        return None
    if isinstance(example, (list, dict)):
        return example
    return None


def load_gepa_config() -> GEPAConfig:
    """从环境变量加载GEPA配置"""
    load_dotenv()
    return GEPAConfig(
        iterations=int(os.getenv("GEPA_ITERATIONS", "10")),
        pareto_size=int(os.getenv("GEPA_PARETO_SIZE", "180")),
        minibatch_size=int(os.getenv("GEPA_MINIBATCH_SIZE", "4")),
        random_seed=int(os.getenv("GEPA_RANDOM_SEED", "42")) if os.getenv("GEPA_RANDOM_SEED") else None,
        tie_breaker=os.getenv("GEPA_TIE_BREAKER", "PREFER_CHILD"),
    )


def load_exec_model_config(large=False, overrides=None) -> Optional[ModelConfig]:
    """Direct execution configuration; large execution requires an explicit model."""
    load_dotenv()
    overrides = overrides or {}
    prefix = "LARGE_EXEC" if large else "EXEC"
    def value(key, fallback):
        override = overrides.get(f"{prefix.lower()}_{key.lower()}")
        return override if override is not None else (os.getenv(f"{prefix}_{key}") or fallback)
    model = value("MODEL", "" if large else os.getenv("MODEL_NAME", "mimo-v2-flash"))
    if large and not model:
        return None
    return ModelConfig(model_name=model, api_base=value("API_BASE", os.getenv("API_BASE")),
                       api_key=value("API_KEY", os.getenv("API_KEY")),
                       temperature=float(value("TEMPERATURE", os.getenv("TEMPERATURE", "0.7"))),
                       max_tokens=int(value("MAX_TOKENS", os.getenv("MAX_TOKENS", "4000"))),
                       timeout=int(value("TIMEOUT", os.getenv("TIMEOUT", "120"))))
