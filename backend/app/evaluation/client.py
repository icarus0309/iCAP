"""答题函数：demo 确定性作答（离线）与 live 真实端点调用。

本模块只依赖 model_registry 提供的模型结构与调用方注入的取 key 函数，
不 import main.py，避免循环依赖。
"""
from __future__ import annotations

import asyncio
import hashlib
from typing import Callable

import httpx

SYSTEM_PROMPT = "你是一个被测模型，请直接作答，不要解释。只输出最终答案。"

#: 默认采样参数（与 EvalConfig 默认值一致）
DEFAULT_PARAMS = {"max_tokens": 1024, "temperature": 0.0}


class EndpointUnavailableError(RuntimeError):
    """模型端点不可达或返回非 2xx / 无法解析。"""


async def demo_answer(case: dict) -> str:
    """演示模式答题：基于用例 id 的 sha256 确定性地返回三类回答。

    不发起任何真实请求；同一 id 结果恒定（跨进程/跨重启一致），
    因此 demo 任务的综合分可复现，便于测试与演示。
    """
    digest = hashlib.sha256(case["id"].encode("utf-8")).hexdigest()
    r = int(digest[:2], 16) % 10              # 0..9，稳定分布
    if r < 2:
        return "未作答"                        # fail
    keys = case.get("answer_keys") or []
    if r < 5 and keys:
        return keys[0]                         # partial
    return case["reference"]                   # pass（含 format/code 走参考全文）


def live_answer(
    model: dict,
    api_key_of: Callable[[dict], str] | None = None,
    endpoint_of: Callable[[dict], dict] | None = None,
    params: dict | None = None,
) -> Callable[[dict], "asyncio.Future"] | None:
    """绑定模型端点返回真实答题函数；模型无端点返回 None（调用方回退 demo_answer）。

    - `api_key_of`：由调用方注入的取密钥函数（可回落 key_env 环境变量）；
    - `endpoint_of`：由调用方注入的端点选择函数（复用 registry.next_endpoint 轮询）；
    - `params`：采样参数，支持 max_tokens / temperature，其余键忽略。
    """
    endpoints = model.get("endpoints") or []
    if not endpoints:
        return None
    settings = {**DEFAULT_PARAMS, **{k: v for k, v in (params or {}).items()
                                     if k in ("max_tokens", "temperature")}}

    async def answer(case: dict) -> str:
        for attempt in range(3):                       # 失败重试 2 次
            try:
                endpoint = endpoint_of(model) if endpoint_of else endpoints[0]
                url = endpoint["url"].rstrip("/")
                if not url.endswith("/chat/completions"):
                    url = f"{url}/chat/completions"
                payload = {
                    "model": model["id"],
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": case["prompt"]},
                    ],
                    "temperature": settings["temperature"],
                    "max_tokens": settings["max_tokens"],
                    "stream": False,
                }
                headers = {"Content-Type": "application/json"}
                key = api_key_of(endpoint) if api_key_of else (endpoint.get("api_key") or "")
                if key:
                    headers["Authorization"] = f"Bearer {key}"
                async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=15)) as client:
                    response = await client.post(url, headers=headers, json=payload)
                    response.raise_for_status()
                content = response.json().get("choices", [{}])[0].get("message", {}).get("content")
                if content is None:
                    raise KeyError("choices[0].message.content")
                return content
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
                if attempt == 2:
                    raise EndpointUnavailableError(f"模型端点执行失败：{exc}") from exc
                await asyncio.sleep(0.4 * (attempt + 1))
        raise EndpointUnavailableError("unreachable")

    return answer
