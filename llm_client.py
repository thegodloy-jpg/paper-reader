"""统一 LLM 客户端 - 支持 Codex CLI / LiteLLM 多后端调用

提供统一接口，底层可通过配置切换 provider：
- codex: 调用本地 Codex CLI（使用 ChatGPT 订阅）
- litellm: 通过 LiteLLM 调用任意 OpenAI 兼容后端
- raw: 原始 urllib 直接调用（兼容原版行为）
"""

import json
import logging
import os
import subprocess
import urllib.request

# 抑制 LiteLLM 非关键日志 (SSL timeout warnings, "Give Feedback" 等)
logging.getLogger("LiteLLM").setLevel(logging.ERROR)
logging.getLogger("litellm").setLevel(logging.ERROR)
from typing import Optional


# Codex CLI 默认路径（VS Code 扩展自带）
_CODEX_CLI_PATHS = [
    os.path.expanduser(
        r"~\.vscode\extensions\openai.chatgpt-*\bin\windows-x86_64\codex.exe"
    ),
]


def _find_codex_cli() -> Optional[str]:
    """自动查找 Codex CLI 可执行文件路径"""
    import glob
    for pattern in _CODEX_CLI_PATHS:
        matches = sorted(glob.glob(pattern), reverse=True)
        if matches:
            return matches[0]
    # 也尝试 PATH
    try:
        result = subprocess.run(
            ["where", "codex"], capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip().split("\n")[0]
    except Exception:
        pass
    return None


def chat_completion(
    messages: list[dict],
    provider: str = "codex",
    model: str = "",
    base_url: str = "",
    api_key: str = "",
    temperature: float = 0.3,
    max_tokens: int = 4000,
    codex_cli_path: str = "",
) -> Optional[str]:
    """统一的聊天补全接口

    Args:
        messages: OpenAI 格式的消息列表 [{"role": ..., "content": ...}]
        provider: 后端提供商 ("codex" | "litellm" | "raw")
        model: 模型名称
        base_url: API 端点（raw/litellm 使用）
        api_key: API 密钥（raw/litellm 使用）
        temperature: 采样温度
        max_tokens: 最大输出 token 数
        codex_cli_path: Codex CLI 路径（可选，自动查找）

    Returns:
        模型回复文本，失败返回 None
    """
    if provider == "codex":
        return _codex_completion(messages, model, codex_cli_path)
    elif provider == "litellm":
        return _litellm_completion(messages, model, base_url, api_key, temperature, max_tokens)
    elif provider == "raw":
        return _raw_completion(messages, model, base_url, api_key, temperature, max_tokens)
    else:
        raise ValueError(f"不支持的 provider: {provider}")


# =============================================================================
# Codex CLI 后端
# =============================================================================

def _codex_completion(
    messages: list[dict],
    model: str = "",
    codex_cli_path: str = "",
) -> Optional[str]:
    """通过 Codex CLI 的 exec --json 模式完成聊天

    利用 ChatGPT 订阅的模型能力，通过 codex exec 的非交互模式调用。
    使用 --json 输出 JSONL 事件流，从 item.completed 事件中提取回复。
    """
    cli = codex_cli_path or _find_codex_cli()
    if not cli:
        print("  [Codex] 未找到 codex CLI，请确认 OpenAI Codex 扩展已安装")
        return None

    # 将 messages 合并为单个 prompt
    prompt_parts = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            prompt_parts.append(f"[系统指令]\n{content}\n")
        elif role == "user":
            prompt_parts.append(f"[用户请求]\n{content}\n")
        elif role == "assistant":
            prompt_parts.append(f"[AI回复]\n{content}\n")
    prompt_parts.append("[要求] 请直接输出回复内容，不要执行任何命令或修改任何文件。")
    full_prompt = "\n".join(prompt_parts)

    try:
        cmd = [cli, "exec"]
        if model:
            cmd.extend(["-m", model])
        cmd.extend([
            "--sandbox", "read-only",
            "--ephemeral",
            "--skip-git-repo-check",
            "--json",
            "--",
            full_prompt,
        ])

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            encoding="utf-8",
            errors="replace",
        )

        # 解析 JSONL 事件流，提取 item.completed 中的回复文本
        if result.stdout:
            response_parts = []
            for line in result.stdout.strip().split("\n"):
                try:
                    evt = json.loads(line)
                    evt_type = evt.get("type", "")
                    if evt_type == "item.completed":
                        text = evt.get("item", {}).get("text", "")
                        if text:
                            response_parts.append(text)
                    elif evt_type == "turn.failed":
                        err_msg = evt.get("error", {}).get("message", "未知错误")
                        print(f"  [Codex] 调用失败: {err_msg[:200]}")
                        return None
                except json.JSONDecodeError:
                    continue
            if response_parts:
                return "\n".join(response_parts)

        if result.returncode != 0:
            print(f"  [Codex] 执行失败 (exit={result.returncode}): {result.stderr[:200]}")
        return None

    except subprocess.TimeoutExpired:
        print("  [Codex] 执行超时（120秒）")
        return None
    except Exception as e:
        print(f"  [Codex] 错误: {e}")
        return None


# =============================================================================
# LiteLLM 后端
# =============================================================================

def _litellm_completion(
    messages: list[dict],
    model: str = "",
    base_url: str = "",
    api_key: str = "",
    temperature: float = 0.3,
    max_tokens: int = 4000,
) -> Optional[str]:
    """通过 LiteLLM 调用模型

    LiteLLM 支持 100+ 提供商，使用 provider/model 格式指定：
    - github_copilot/gpt-5-mini  (GitHub Copilot 免费，推荐)
    - github_copilot/gpt-4o      (GitHub Copilot)
    - github/gpt-4o              (GitHub Models 免费)
    - openai/gpt-4o              (OpenAI Platform)
    - anthropic/claude-3-opus    (Anthropic)
    """
    try:
        import litellm  # type: ignore[import-not-found]
        litellm.drop_params = True
        litellm.suppress_debug_info = True

        kwargs = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "timeout": 120,
        }
        if base_url:
            kwargs["api_base"] = base_url
        if api_key:
            kwargs["api_key"] = api_key

        response = litellm.completion(**kwargs)
        return response.choices[0].message.content

    except ImportError:
        print("  [LiteLLM] 未安装 litellm，请运行: pip install litellm")
        return None
    except Exception as e:
        print(f"  [LiteLLM] 调用失败: {e}")
        return None


# =============================================================================
# 原始 urllib 后端（兼容原版）
# =============================================================================

def _raw_completion(
    messages: list[dict],
    model: str = "",
    base_url: str = "",
    api_key: str = "",
    temperature: float = 0.3,
    max_tokens: int = 4000,
) -> Optional[str]:
    """原始 urllib 调用 OpenAI 兼容 API"""
    if not api_key:
        api_key = os.environ.get("PAPER_READER_API_KEY", "")
    if not api_key:
        try:
            result = subprocess.run(
                ["gh", "auth", "token"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                api_key = result.stdout.strip()
        except Exception:
            pass
    if not api_key:
        print("  [Raw] 未配置 API 密钥")
        return None

    payload = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode("utf-8")

    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    try:
        import time
        import urllib.error
        max_retries = 5
        for attempt in range(max_retries):
            try:
                req = urllib.request.Request(
                    url, data=payload, headers=headers, method="POST",
                )
                with urllib.request.urlopen(req, timeout=90) as response:
                    result = json.loads(response.read())
                    return result["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    retry_after = int(e.headers.get("Retry-After", 0))
                    wait = max(retry_after, 10 * (2 ** attempt))
                    wait = min(wait, 120)
                    print(f"  [429限流] 等待 {wait}s 后重试 ({attempt + 1}/{max_retries})")
                    time.sleep(wait)
                elif attempt < max_retries - 1:
                    print(f"  [重试 {attempt + 1}/{max_retries}] HTTP {e.code}")
                    time.sleep(5)
                else:
                    raise
            except Exception as e:
                if attempt < max_retries - 1:
                    print(f"  [重试 {attempt + 1}/{max_retries}] {e}")
                    time.sleep(5)
                else:
                    raise
    except Exception as e:
        print(f"  [Raw API 失败] {e}")
        return None


def _resolve_api_key(key_spec: str) -> str:
    """解析 API key 配置值，支持 env:VAR_NAME 语法"""
    if not key_spec:
        return ""
    if key_spec.startswith("env:"):
        return os.environ.get(key_spec[4:], "")
    return key_spec


def chat_completion_with_fallback(
    messages: list[dict],
    config: dict,
    temperature: float = 0.3,
    max_tokens: int = 4000,
) -> Optional[str]:
    """带 fallback 链的聊天补全

    先尝试主 provider，失败后依次尝试 config["ai"]["fallback"] 中的后备提供商。

    config["ai"] 示例:
        provider: codex
        model: gpt-5.4
        fallback:
          - provider: raw
            model: gpt-4o
            base_url: https://models.inference.ai.azure.com
            api_key: "env:GITHUB_TOKEN"
          - provider: litellm
            model: github/gpt-4o
    """
    ai_cfg = config.get("ai", {})

    # 主 provider 先试
    result = chat_completion(
        messages=messages,
        provider=ai_cfg.get("provider", "codex"),
        model=ai_cfg.get("model", ""),
        base_url=ai_cfg.get("base_url", ""),
        api_key=_resolve_api_key(ai_cfg.get("api_key", "")),
        temperature=temperature,
        max_tokens=max_tokens,
        codex_cli_path=ai_cfg.get("codex_cli_path", ""),
    )
    if result:
        return result

    # 尝试 fallback 链
    fallback_list = ai_cfg.get("fallback", [])
    for i, fb in enumerate(fallback_list):
        fb_provider = fb.get("provider", "raw")
        fb_model = fb.get("model", "")
        print(f"  [Fallback {i + 1}/{len(fallback_list)}] 尝试 {fb_provider}/{fb_model}...")
        result = chat_completion(
            messages=messages,
            provider=fb_provider,
            model=fb_model,
            base_url=fb.get("base_url", ""),
            api_key=_resolve_api_key(fb.get("api_key", "")),
            temperature=temperature,
            max_tokens=max_tokens,
            codex_cli_path=fb.get("codex_cli_path", ""),
        )
        if result:
            return result

    return None
