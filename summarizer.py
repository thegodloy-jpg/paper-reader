"""AI 摘要生成模块 - 通过统一 LLM 客户端生成论文摘要"""

from typing import Optional

from .arxiv_fetcher import Paper
from .llm_client import chat_completion, chat_completion_with_fallback


SYSTEM_PROMPT = """你是一位专注于大模型推理优化与加速的AI研究助手。
请根据提供的论文标题和摘要，生成一份结构化的论文阅读笔记。

要求：
1. 用{language}撰写（原始摘要部分保留英文原文）
2. 严格按照以下格式输出，不要添加额外内容：

## 一句话总结
（用一句话概括论文核心贡献）

## 原始摘要（Abstract）
（完整保留英文原始摘要，不做任何修改）

## 摘要翻译
（将上面的英文摘要翻译成准确流畅的中文）

## 方法概览图
（用 Mermaid 流程图描述论文的核心方法架构或流程。使用 ```mermaid 代码块。图中节点文字用中文。保持简洁，5-10个节点即可）

## 研究背景
（该研究所处的领域背景，当前技术发展到什么阶段，存在哪些已知挑战或瓶颈）

## 研究动机
（这篇论文具体要解决什么问题？为什么现有方法不够好？为什么重要？）

## 核心方法
（论文提出了什么方法/技术？关键创新点是什么？用要点列出）

## 主要结果
（实验结果如何？相比baseline有多大提升？列出关键数字）

## 结论
（作者的主要结论是什么？方法的局限性？未来可能的研究方向？用2-3句话精简概括）

## 与我的研究相关性
（对大模型推理优化加速领域的启发/可借鉴之处，具体到可以怎么用）

## 代码与复现
（如果提供了代码仓库链接，列出并说明；如果没有，说明"暂无开源代码"）

## 关键术语
（列出3-5个关键术语及简要解释）"""


def summarize_paper(
    paper: Paper,
    provider: str = "codex",
    base_url: str = "",
    api_key: str = "",
    model: str = "",
    language: str = "中文",
    codex_cli_path: str = "",
    config: dict | None = None,
) -> Optional[str]:
    """使用统一 LLM 客户端生成论文摘要

    Args:
        paper: 论文对象
        provider: LLM 提供商 ("codex" | "litellm" | "raw")
        base_url: API 端点 (raw/litellm 使用)
        api_key: API 密钥 (raw/litellm 使用)
        model: 模型名称
        language: 输出语言
        codex_cli_path: Codex CLI 路径
        config: 完整配置（传入时启用 fallback 链）

    Returns:
        生成的摘要文本，失败返回 None
    """
    code_info = ""
    code_url = getattr(paper, '_code_url', None)
    if code_url:
        code_info = f"\n代码仓库: {code_url}"

    user_prompt = f"""论文标题: {paper.title}

作者: {', '.join(paper.authors[:5])}

原始摘要:
{paper.abstract}

分类: {', '.join(paper.categories[:3])}{code_info}"""

    system_prompt = SYSTEM_PROMPT.replace("{language}", language)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    if config:
        return chat_completion_with_fallback(
            messages=messages,
            config=config,
            temperature=0.3,
            max_tokens=4000,
        )

    return chat_completion(
        messages=messages,
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=0.3,
        max_tokens=4000,
        codex_cli_path=codex_cli_path,
    )


def generate_fallback_summary(paper: Paper) -> str:
    """当 AI 不可用时，生成基于原始摘要的结构化笔记"""
    return f"""## 一句话总结
{paper.title}

## 原始摘要（Abstract）
{paper.abstract}

## 摘要翻译
（AI 摘要不可用，请手动阅读）

## 核心方法
待阅读

## 主要结果
待阅读
"""
