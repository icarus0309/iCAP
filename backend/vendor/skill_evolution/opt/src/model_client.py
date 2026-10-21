"""
模型客户端封装
"""
import json
import socket
import time
from typing import List, Dict, Any, Optional
import urllib.request
import urllib.error
from http.client import RemoteDisconnected, IncompleteRead
from src.config import ModelConfig


def normalize_chat_completions_url(api_base: Optional[str]) -> str:
    """Accept either a root API URL or a full chat completions endpoint."""
    base = (api_base or "https://api.openai.com/v1/chat/completions").strip()
    if base.endswith("/chat/completions"):
        return base
    base = base.rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


class ModelClient:
    """LLM模型客户端"""

    def __init__(self, config: ModelConfig):
        self.config = config

    def generate(self, messages: List[Dict[str, str]], **kwargs) -> str:
        """生成响应（带重试机制）"""
        max_retries = kwargs.get("max_retries", 3)
        timeout = kwargs.get("timeout", self.config.timeout)  # 从 kwargs 获取 timeout
        retry_count = 0

        url = normalize_chat_completions_url(self.config.api_base)

        payload = {
            "model": self.config.model_name,
            "messages": messages,
            "temperature": kwargs.get("temperature", self.config.temperature),
            "max_tokens": kwargs.get("max_tokens", self.config.max_tokens),
        }

        # 对于Qwen3系列模型，非流式调用需要设置enable_thinking为false
        if "qwen3" in self.config.model_name.lower():
            payload["enable_thinking"] = kwargs.get("enable_thinking", False)

        # 支持传递额外的参数
        extra_params = kwargs.get("extra_params", {})
        for key, value in extra_params.items():
            if key not in payload:
                payload[key] = value

        data = json.dumps(payload).encode('utf-8')

        headers = {
            "Content-Type": "application/json",
        }

        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        req = urllib.request.Request(url, data=data, headers=headers, method='POST')

        print(f"[HTTP请求] 模型: {self.config.model_name}, URL: {url}, timeout: {timeout}秒")

        while True:
            try:
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    result = json.loads(response.read().decode('utf-8'))
                    choice = result["choices"][0]
                    message = choice.get("message", {}) or {}
                    content = message.get("content") or ""
                    print(f"[HTTP响应] 成功, 内容长度: {len(content)}")
                    # 空响应重试
                    if not content or not content.strip():
                        finish_reason = choice.get("finish_reason", "")
                        reasoning = message.get("reasoning_content") or ""
                        if finish_reason == "length" and reasoning:
                            print(
                                f"[空响应] finish_reason=length, reasoning_content={len(reasoning)}字符 —— "
                                f"max_tokens 被思维链(reasoning)耗尽，content 为空。请增大 max_tokens（如 EVAL_MAX_TOKENS/GEN_MAX_TOKENS）。"
                            )
                        else:
                            print(f"[空响应] finish_reason={finish_reason}, reasoning_content={len(reasoning)}字符")
                        retry_count += 1
                        if retry_count > max_retries:
                            print(f"[重试耗尽] {max_retries} 次空响应，放弃")
                            return ""
                        wait_time = 2 ** retry_count
                        print(f"[空响应] 第{retry_count}次重试，等待{wait_time}秒...")
                        time.sleep(wait_time)
                        continue
                    return content

            except urllib.error.HTTPError as e:
                # HTTP 状态码错误（401/404/429 等）——打印响应体便于定位，再重试
                error_body = ""
                try:
                    error_body = e.read().decode('utf-8')
                except Exception:
                    pass

                retry_count += 1
                if retry_count > max_retries:
                    print(f"\n{'='*60}")
                    print(f"模型调用失败 - HTTP {e.code} 错误（已重试 {max_retries} 次）")
                    print(f"{'='*60}")
                    print(f"URL: {url}")
                    print(f"模型: {self.config.model_name}")
                    print(f"状态码: {e.code} {e.reason}")
                    if error_body:
                        try:
                            error_json = json.loads(error_body)
                            print(f"错误详情: {json.dumps(error_json, ensure_ascii=False, indent=2)}")
                        except Exception:
                            print(f"响应体: {error_body[:500]}")
                    print(f"{'='*60}\n")
                    return ""

                wait_time = 2 ** retry_count
                print(f"[HTTP {e.code}] 第{retry_count}次重试，等待{wait_time}秒... {error_body[:200]}")
                time.sleep(wait_time)

            except (TimeoutError, socket.timeout, urllib.error.URLError, RemoteDisconnected, IncompleteRead, json.JSONDecodeError) as e:
                # 超时、网络错误、连接中断、JSON解析错误 - 都重试
                retry_count += 1
                if retry_count > max_retries:
                    print(f"\n[重试耗尽] 已重试 {max_retries} 次，仍失败")
                    return ""
                wait_time = 2 ** retry_count  # 指数退避: 2, 4, 8秒
                print(f"\n[{type(e).__name__}] 第{retry_count}次重试，等待{wait_time}秒... 错误: {e}")
                time.sleep(wait_time)
            except Exception as e:
                # 其他未知错误 - 也重试
                import traceback
                retry_count += 1
                if retry_count > max_retries:
                    print(f"\n{'='*60}")
                    print(f"模型调用失败 - 未知错误（已重试 {max_retries} 次）")
                    print(f"{'='*60}")
                    print(f"URL: {url}")
                    print(f"模型: {self.config.model_name}")
                    print(f"错误类型: {type(e).__name__}")
                    print(f"错误信息: {e}")
                    print(f"错误堆栈:")
                    traceback.print_exc()
                    print(f"{'='*60}\n")
                    return ""

                wait_time = 2 ** retry_count
                print(f"\n[{type(e).__name__}] 第{retry_count}次重试，等待{wait_time}秒... 错误: {e}")
                time.sleep(wait_time)

    def generate_with_system(self, system_prompt: str, user_message: str, **kwargs) -> str:
        """使用系统提示生成响应"""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ]
        return self.generate(messages, **kwargs)


class MockModelClient(ModelClient):
    """模拟模型客户端，用于测试"""

    def __init__(self, config: ModelConfig):
        super().__init__(config)
        self.call_count = 0

    def generate(self, messages: List[Dict[str, str]], **kwargs) -> str:
        """模拟生成响应"""
        self.call_count += 1

        # 根据消息内容返回模拟响应
        last_message = messages[-1]["content"] if messages else ""

        if "prompt" in last_message.lower() or "提示" in last_message:
            return "请根据以下问题给出准确、详细的回答：{input}"
        elif "evaluate" in last_message.lower() or "评估" in last_message:
            return "0.8"
        else:
            return "这是一个模拟的响应。"

    def generate_with_system(self, system_prompt: str, user_message: str, **kwargs) -> str:
        """模拟使用系统提示生成响应"""
        return self.generate([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message}
        ], **kwargs)
