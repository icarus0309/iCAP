"""Pre-condition hook for self-evolution cases.

Injects entity states into Mock HA before each case runs,
so the agent sees the expected initial environment.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional


class PreConditionHook(ABC):
    """Base interface for pre-condition setup/teardown."""

    @abstractmethod
    def setup(self, pre_condition: list) -> None:
        """Set up pre-condition before running a case."""

    @abstractmethod
    def teardown(self) -> None:
        """Clean up after the case."""


class NoOpPreConditionHook(PreConditionHook):
    """No-op hook when pre-condition is disabled."""

    def setup(self, pre_condition: list) -> None:
        pass

    def teardown(self) -> None:
        pass


class HAPreConditionHook(PreConditionHook):
    """Inject pre-condition via Mock HA REST API."""

    def __init__(self, url: str = "http://127.0.0.1:8124", token: str = "mock-token"):
        self.url = url
        self.token = token
        self._adapter = None

    @property
    def adapter(self):
        if self._adapter is None:
            from ha_adapter import HAAdapter
            self._adapter = HAAdapter(url=self.url, token=self.token)
        return self._adapter

    def setup(self, pre_condition: list) -> None:
        if not pre_condition:
            return
        if not self.adapter.ping():
            print(f"[PreCondition] 警告: Mock HA ({self.url}) 不可达，跳过注入")
            return

        # ── 注入 ──
        entities = [p.get("entity_id", "?") for p in pre_condition]
        print(f"[PreCondition] 注入 {len(pre_condition)} 个实体: {', '.join(entities)}")
        result = self.adapter.inject_pre_condition(pre_condition)
        if "error" in result:
            print(f"[PreCondition] 注入失败: {result['error']}")
            return

        # ── 验证 ──
        warnings = self.adapter.verify_pre_condition(pre_condition)
        if warnings:
            for w in warnings:
                print(f"[PreCondition] 验证不一致: {w}")
        else:
            print(f"[PreCondition] 验证通过，前置条件已就绪")

    def teardown(self) -> None:
        if self._adapter is not None:
            self._adapter.reset_states()
            print(f"[PreCondition] 环境已重置")


def create_pre_condition_hook(
    enabled: bool = False,
    url: str = "http://127.0.0.1:8124",
    token: str = "mock-token",
) -> PreConditionHook:
    """Factory: create a pre-condition hook.

    Args:
        enabled: Enable the hook (True → HAPreConditionHook, False → NoOp).
        url: Mock HA base URL.
        token: Mock HA auth token.
    """
    if enabled:
        return HAPreConditionHook(url=url, token=token)
    return NoOpPreConditionHook()
