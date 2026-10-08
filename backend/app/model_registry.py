"""Model catalog, editable endpoint configuration, and round-robin routing."""
from __future__ import annotations

import copy
import os
import threading
import time
import uuid
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator

from .store import Store


DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_THINKING = {
    "enabled": {"thinking": {"type": "enabled"}},
    "disabled": {"thinking": {"type": "disabled"}},
}


class EndpointInput(BaseModel):
    endpoint_id: str | None = None
    url: str = Field(min_length=1, max_length=2048)
    api_key: str = Field(default="", max_length=4096)
    thinking_schema: dict = Field(default_factory=dict)
    self_deployed: bool = False
    max_context_length: int = Field(default=32768, gt=0, le=2_000_000)
    top_k: int = Field(default=40, ge=0, le=1000)
    top_p: float = Field(default=1.0, ge=0, le=1)
    temperature: float = Field(default=1.0, ge=0, le=2)

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("API URL 必须是不含账号、查询参数的 HTTP(S) 地址")
        return value

    @field_validator("thinking_schema")
    @classmethod
    def valid_thinking_schema(cls, value: dict) -> dict:
        if value and (set(value) != {"enabled", "disabled"} or
                      not all(isinstance(value[mode], dict) for mode in ("enabled", "disabled"))):
            raise ValueError('思考 Schema 须为包含 "enabled" 和 "disabled" 对象的 JSON')
        if any({"model", "messages", "stream"} & set(value[mode]) for mode in value):
            raise ValueError("思考 Schema 不能覆盖 model、messages 或 stream")
        return value


class ModelInput(BaseModel):
    id: str = Field(min_length=1, max_length=150)
    name: str = Field(min_length=1, max_length=100)
    provider: str = Field(min_length=1, max_length=60)
    logo_url: str = Field(default="", max_length=2048)
    visibility: Literal["private", "public"] = "private"
    endpoints: list[EndpointInput] = Field(min_length=1, max_length=20)

    @field_validator("id", "name", "provider")
    @classmethod
    def trimmed(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("不能为空")
        return value

    @field_validator("logo_url")
    @classmethod
    def valid_logo_url(cls, value: str) -> str:
        value = value.strip()
        if value:
            parsed = urlsplit(value)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ValueError("Logo URL 必须为 HTTP(S) 地址")
        return value


class ModelRegistry:
    def __init__(self, store: Store):
        self.store = store
        self._route_lock = threading.Lock()
        self._route_counts: dict[str, int] = {}
        self._catalog_lock = threading.Lock()
        self._catalog_cache: list[dict] = []
        self._catalog_at = 0.0

    def _remote_models(self) -> list[dict]:
        if not os.getenv("DEEPSEEK_API_KEY", "").strip():
            return []
        with self._catalog_lock:
            if self._catalog_cache and time.monotonic() - self._catalog_at < 60:
                return copy.deepcopy(self._catalog_cache)
            for attempt in range(2):
                try:
                    response = httpx.get(
                        f"{DEEPSEEK_BASE_URL}/models",
                        headers={"Authorization": f"Bearer {os.environ['DEEPSEEK_API_KEY'].strip()}"},
                        timeout=20,
                    )
                    response.raise_for_status()
                    data = response.json().get("data")
                    if not isinstance(data, list):
                        raise ValueError("invalid model catalog")
                    break
                except (httpx.HTTPError, ValueError, AttributeError) as exc:
                    if attempt == 1:
                        if self._catalog_cache:
                            return copy.deepcopy(self._catalog_cache)
                        raise HTTPException(502, "无法从 DeepSeek 获取模型目录，请检查 API 配置或网络") from exc

        models = []
        for item in data:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            model_id = item["id"]
            inputs = item.get("input_modalities") or ["text"]
            models.append({
                "id": model_id,
                "name": item.get("name") or model_id,
                "provider": "DeepSeek",
                "logo_url": "",
                "source": "DeepSeek API",
                "owner_user_id": None,
                "visibility": "public",
                "input_modalities": inputs,
                "output_modalities": item.get("output_modalities") or ["text"],
                "max_output_tokens": item.get("max_output_tokens"),
                "effort_levels": (item.get("effort") or {}).get("supported_levels", []),
                "scores": [],
                "endpoints": [{
                    "endpoint_id": "deepseek-default",
                    "url": DEEPSEEK_BASE_URL,
                    "key_env": "DEEPSEEK_API_KEY",
                    "thinking_schema": copy.deepcopy(DEEPSEEK_THINKING),
                    "self_deployed": False,
                    "max_context_length": item.get("context_window") or 32768,
                    "top_k": 40,
                    "top_p": 1.0,
                    "temperature": 1.0,
                }],
            })
        with self._catalog_lock:
            self._catalog_cache = copy.deepcopy(models)
            self._catalog_at = time.monotonic()
        return models

    @staticmethod
    def can_view(model: dict, actor: dict | None = None) -> bool:
        if actor is None or actor.get("role") == "admin":
            return True
        return model.get("visibility", "public") == "public" or model.get("owner_user_id") == actor.get("id")

    @staticmethod
    def can_edit(model: dict, actor: dict | None = None) -> bool:
        if actor is None or actor.get("role") == "admin":
            return True
        return bool(model.get("owner_user_id")) and model.get("owner_user_id") == actor.get("id")

    @classmethod
    def require_edit(cls, model: dict, actor: dict | None = None) -> None:
        if not cls.can_edit(model, actor):
            raise HTTPException(403, "无权修改此模型")

    def list_models(self, actor: dict | None = None) -> list[dict]:
        stored = self.store.snapshot()["models"]
        try:
            remote = self._remote_models()
        except HTTPException:
            if not stored:
                raise
            remote = []
        by_id = {model["id"]: model for model in remote}
        for model in stored:
            by_id[model["id"]] = {**by_id.get(model["id"], {}), **model}
        return [model for model in by_id.values() if self.can_view(model, actor)]

    def get_model(self, model_id: str, actor: dict | None = None) -> dict:
        stored = next((item for item in self.store.snapshot()["models"] if item["id"] == model_id), None)
        if stored and stored.get("endpoints"):
            if not self.can_view(stored, actor):
                raise HTTPException(404, "模型不存在")
            return stored
        model = next((item for item in self.list_models(actor) if item["id"] == model_id), None)
        if model is None:
            raise HTTPException(404, "模型不存在")
        return model

    @staticmethod
    def public(model: dict, actor: dict | None = None) -> dict:
        if not ModelRegistry.can_view(model, actor):
            raise HTTPException(404, "模型不存在")
        result = {key: copy.deepcopy(value) for key, value in model.items() if key != "endpoints"}
        result["endpoints"] = []
        for endpoint in model.get("endpoints", []):
            visible = {key: copy.deepcopy(value) for key, value in endpoint.items()
                       if key not in ("api_key", "key_env")}
            visible["has_api_key"] = bool(endpoint.get("api_key") or
                                          (endpoint.get("key_env") and os.getenv(endpoint["key_env"])))
            result["endpoints"].append(visible)
        result["context_length"] = max((item["max_context_length"] for item in result["endpoints"]), default=0)
        result["modality"] = "多模态" if any(m in ("image", "audio", "video") for m in result.get("input_modalities", [])) else "文本"
        result["visibility"] = model.get("visibility", "public")
        result["owner_user_id"] = model.get("owner_user_id")
        if actor is not None:
            result["can_edit"] = ModelRegistry.can_edit(model, actor)
        return result

    def save(self, body: ModelInput, original_id: str | None = None, actor: dict | None = None) -> dict:
        if original_id is not None and body.id != original_id:
            raise HTTPException(422, "模型 ID 注册后不可修改")
        try:
            available = self.list_models()
        except HTTPException:
            if original_id is not None:
                raise
            available = self.store.snapshot()["models"]
        existing = next((m for m in available if m["id"] == body.id), None)
        if original_id is None and existing is not None:
            raise HTTPException(409, "模型 ID 已存在")
        if original_id is not None and existing is None:
            raise HTTPException(404, "模型不存在")
        if original_id is not None:
            self.require_edit(existing, actor)
        old_endpoints = {e["endpoint_id"]: e for e in (existing or {}).get("endpoints", [])}
        endpoints = []
        seen = set()
        for item in body.endpoints:
            endpoint = item.model_dump()
            endpoint_id = endpoint.pop("endpoint_id") or uuid.uuid4().hex
            if endpoint_id in seen:
                raise HTTPException(422, "端点 ID 不能重复")
            seen.add(endpoint_id)
            old = old_endpoints.get(endpoint_id)
            if not endpoint["api_key"] and old and old["url"] == endpoint["url"]:
                endpoint["api_key"] = old.get("api_key", "")
                if old.get("key_env"):
                    endpoint["key_env"] = old["key_env"]
            endpoint["endpoint_id"] = endpoint_id
            endpoints.append(endpoint)

        if existing is not None and "visibility" not in body.model_fields_set:
            visibility = existing.get("visibility", "public")
        elif "visibility" in body.model_fields_set:
            visibility = body.visibility
        else:
            visibility = "public" if actor is None else "private"

        record = {
            "id": body.id,
            "name": body.name,
            "provider": body.provider,
            "logo_url": body.logo_url,
            "source": (existing or {}).get("source", "用户注册"),
            "owner_user_id": existing.get("owner_user_id") if existing is not None else (actor or {}).get("id"),
            "visibility": visibility,
            "endpoints": endpoints,
            "scores": (existing or {}).get("scores", []),
        }

        def op(data: dict) -> dict:
            rows = data["models"]
            index = next((i for i, row in enumerate(rows) if row["id"] == body.id), None)
            if original_id is None and index is not None:
                raise HTTPException(409, "模型 ID 已存在")
            if index is None:
                rows.append(record)
            else:
                self.require_edit(rows[index], actor)
                rows[index] = record
            return record

        return self.public(self.store.update(op), actor)

    def delete(self, model_id: str, actor: dict | None = None) -> dict:
        def op(data: dict) -> dict:
            rows = data["models"]
            model = next((item for item in rows if item["id"] == model_id), None)
            if model is None:
                raise HTTPException(404, "模型不存在")
            self.require_edit(model, actor)
            if any(item["model_id"] == model_id for item in
                   data["evaluations"] + data["deployments"] + data["school_jobs"]):
                raise HTTPException(409, "模型已被任务或申请引用")
            data["models"] = [item for item in rows if item["id"] != model_id]
            return {"deleted": model_id}

        return self.store.update(op)

    def next_endpoint(self, model: dict) -> dict:
        endpoints = model.get("endpoints") or []
        if not endpoints:
            raise HTTPException(422, "模型尚未配置 API 端点")
        with self._route_lock:
            index = self._route_counts.get(model["id"], 0)
            self._route_counts[model["id"]] = index + 1
        return endpoints[index % len(endpoints)]

    @staticmethod
    def api_key(endpoint: dict) -> str:
        return endpoint.get("api_key") or os.getenv(endpoint.get("key_env", ""), "")
