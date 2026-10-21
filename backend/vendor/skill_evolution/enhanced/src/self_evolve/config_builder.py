"""Single source of truth for building the full self-evolve configuration.

Both the CLI entry point (``optimize_skill.py`` / ``scripts/run_self_evolve.py``)
and the web dashboard should converge on the same defaults read from ``.env``.
This module reads the same environment variables the CLI launcher uses and
produces a complete ``SelfEvolveConfig`` (Hermes + large-model + GEPA + outcome
split + executor), so the web no longer builds a partial/decoupled config.

Request-level overrides (e.g. from a web form) can be passed as a dict.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from ..config import (
    load_dotenv,
    load_eval_model_config,
    load_gen_model_config,
    load_exec_model_config,
)
from ..model_client import ModelClient
from .execution.executor import load_executor_config_from_env
from .execution.hermes_runner import HermesCliConfig
from .orchestrator import SelfEvolveConfig


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: Optional[int] = None) -> Optional[int]:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _parse_env_pairs(value: str) -> Dict[str, str]:
    """Parse ``KEY=VALUE`` pairs separated by commas/semicolons/newlines."""
    out: Dict[str, str] = {}
    for chunk in (value or "").replace(";", ",").replace("\n", ",").split(","):
        chunk = chunk.strip()
        if "=" not in chunk:
            continue
        key, val = chunk.split("=", 1)
        key = key.strip()
        if key:
            out[key] = val.strip()
    return out


@dataclass
class BuiltEvolveConfig:
    """Complete bundle produced by :func:`build_self_evolve_config`."""

    config: SelfEvolveConfig
    eval_model: Optional[ModelClient]
    gen_model: Optional[ModelClient]


def _build_hermes_config(overrides: Optional[Dict[str, Any]] = None) -> HermesCliConfig:
    overrides = overrides or {}
    return HermesCliConfig(
        hermes_bin=_env("HERMES_BIN", "hermes"),
        hermes_model=overrides.get("hermes_model") or _env("HERMES_MODEL") or None,
        hermes_provider=_env("HERMES_PROVIDER") or None,
        hermes_base_url=_env("HERMES_BASE_URL") or None,
        hermes_api_key=_env("HERMES_API_KEY") or None,
        hermes_timeout=int(overrides.get("hermes_timeout") or _env("HERMES_TIMEOUT", "300")),
        hermes_env=_parse_env_pairs(_env("HERMES_ENV")),
        source_hermes_home=_env("HERMES_HOME") or None,
        seed_user_config=not _env_bool("HERMES_NO_SEED_USER_CONFIG", False),
        toolsets=_env("HERMES_TOOLSETS") or None,
        max_turns=_env_int("HERMES_MAX_TURNS"),
        ignore_user_config=_env_bool("HERMES_IGNORE_USER_CONFIG", False),
        ignore_rules=_env_bool("HERMES_IGNORE_RULES", False),
        yolo=_env_bool("HERMES_YOLO", True),
        executor_config=load_executor_config_from_env(),
    )


def _build_large_hermes_config() -> Optional[HermesCliConfig]:
    model = _env("LARGE_HERMES_MODEL")
    if not model:
        return None
    return HermesCliConfig(
        hermes_bin=_env("HERMES_BIN", "hermes"),
        hermes_model=model,
        hermes_provider=_env("LARGE_HERMES_PROVIDER") or _env("HERMES_PROVIDER") or None,
        hermes_base_url=_env("LARGE_HERMES_BASE_URL") or _env("HERMES_BASE_URL") or None,
        hermes_api_key=_env("LARGE_HERMES_API_KEY") or _env("HERMES_API_KEY") or None,
        hermes_timeout=int(_env("HERMES_TIMEOUT", "300")),
        hermes_env=_parse_env_pairs(_env("LARGE_HERMES_ENV")),
        source_hermes_home=_env("LARGE_HERMES_HOME") or _env("HERMES_HOME") or None,
        seed_user_config=not _env_bool("HERMES_NO_SEED_USER_CONFIG", False),
        toolsets=_env("HERMES_TOOLSETS") or None,
        max_turns=_env_int("HERMES_MAX_TURNS"),
        ignore_user_config=_env_bool("HERMES_IGNORE_USER_CONFIG", False),
        ignore_rules=_env_bool("HERMES_IGNORE_RULES", False),
        yolo=_env_bool("HERMES_YOLO", True),
        executor_config=load_executor_config_from_env(),
    )


def build_self_evolve_config(overrides: Optional[Dict[str, Any]] = None) -> BuiltEvolveConfig:
    """Build the full self-evolve configuration from ``.env`` (+ request overrides)."""
    load_dotenv()
    overrides = overrides or {}

    hermes_config = _build_hermes_config(overrides)
    large_hermes_config = _build_large_hermes_config()

    def _int_override(key: str, env_key: str, default: Optional[int]) -> Optional[int]:
        if key in overrides and overrides[key] is not None:
            return int(overrides[key])
        return _env_int(env_key, default)

    def _float_override(key: str, env_key: str, default: float) -> float:
        if key in overrides and overrides[key] is not None:
            return float(overrides[key])
        return _env_float(env_key, default)

    def _bool_override(key: str, env_key: str, default: bool) -> bool:
        if key in overrides and overrides[key] is not None:
            value = overrides[key]
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "y", "on"}
            return bool(value)
        return _env_bool(env_key, default)

    config = SelfEvolveConfig(
        **build_direct_options(overrides),
        rounds=int(overrides.get("rounds") or _env("SELF_EVOLVE_ROUNDS", "6")),
        threshold=float(overrides.get("threshold") or _env("SELF_EVOLVE_THRESHOLD", "85")),
        cases_limit=_int_override("cases_limit", "SELF_EVOLVE_CASES_LIMIT", None),
        output_dir=_env("SELF_EVOLVE_OUTPUT_DIR", "output"),
        run_id="",
        hermes=hermes_config,
        large_hermes=large_hermes_config,
        result_threshold=_float_override("result_threshold", "RESULT_THRESHOLD", 80.0),
        enable_pre_condition=_bool_override("enable_pre_condition", "ENABLE_PRE_CONDITION", False),
        mock_ha_url=overrides.get("mock_ha_url") or _env("MOCK_HA_URL", "http://127.0.0.1:8124"),
        mock_ha_token=overrides.get("mock_ha_token") or _env("MOCK_HA_TOKEN", "mock-token"),
        max_steps=int(overrides.get("max_steps") or _env("MAX_STEPS", "5")),
        outcome_split_threshold=_env_float("OUTCOME_SPLIT_THRESHOLD", 70.0),
        gepa_iterations=int(overrides["gepa_iterations"] if overrides.get("gepa_iterations") is not None else _env("GEPA_ITERATIONS", "3")),
        gepa_minibatch_size=int(_env("GEPA_MINIBATCH_SIZE", "5")),
        gepa_max_metric_calls=_env_int("GEPA_MAX_METRIC_CALLS"),
        stall_threshold=_float_override("stall_threshold", "STALL_THRESHOLD", 2.0),
        stall_rounds=int(overrides.get("stall_rounds") or _env("STALL_ROUNDS", "2")),
    )

    eval_config = load_eval_model_config()
    gen_config = load_gen_model_config()
    eval_model = ModelClient(eval_config) if eval_config.api_base and eval_config.api_key else None
    gen_model = ModelClient(gen_config) if gen_config.api_base and gen_config.api_key else None

    return BuiltEvolveConfig(config=config, eval_model=eval_model, gen_model=gen_model)


def build_direct_options(overrides=None):
    load_dotenv()
    overrides = overrides or {}
    mode = overrides.get("exec_mode") or _env("SKILL_EXEC_MODE", "hermes")
    if mode not in {"hermes", "direct"}:
        raise ValueError("SKILL_EXEC_MODE must be hermes or direct")
    result = {"exec_mode": mode, "direct_judge_prompt": overrides.get("direct_judge_prompt") or _env("DIRECT_JUDGE_PROMPT")}
    if mode == "direct":
        for large, key in ((False, "exec_model_client"), (True, "large_exec_model_client")):
            config = load_exec_model_config(large, overrides)
            if config and (not config.api_base or not config.api_key):
                raise ValueError("missing API_BASE/API_KEY for " + ("LARGE_EXEC" if large else "EXEC"))
            result[key] = ModelClient(config) if config else None
    return result
