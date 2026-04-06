"""Obsidian 笔记写入模块 - 将论文摘要保存为 Obsidian 兼容的 Markdown 文件"""

import os
import re
from datetime import datetime, timezone
from typing import Optional

from .arxiv_fetcher import Paper


# 默认分类配置（当 config.yaml 中未配置 categories 时使用）
DEFAULT_CATEGORIES = [
    # 注意：按顺序匹配，推测解码放最前（其论文常含 sparse/student 等词易误分到剪枝/蒸馏）
    {"name": "推测解码", "icon": "🎯", "keywords": ["speculative decoding", "speculative sampling", "draft model", "draft-verify", "token tree", "tree attention", "medusa", "eagle", "lookahead decoding"]},
    {"name": "量化", "icon": "🗜️", "keywords": ["quantization", "int4", "int8", "gptq", "awq", "mixed-precision", "low-bit", "4-bit", "2-bit", "1-bit", "weight-only quantization", "post-training quantization", "ptq", "gguf", "fp8", "fp4"]},
    {"name": "剪枝", "icon": "✂️", "keywords": ["pruning", "weight pruning", "structured pruning", "unstructured pruning", "sparsity ratio", "sparse model", "network pruning", "magnitude pruning"]},
    {"name": "蒸馏", "icon": "🧪", "keywords": ["knowledge distillation", "model distillation", "teacher-student", "distill", "student model training"]},
    {"name": "压缩", "icon": "📦", "keywords": ["svd", "low-rank", "model compression", "decomposition", "weight sharing", "compact model"]},
    {"name": "KV缓存", "icon": "💾", "keywords": ["kv cache", "cache optimization", "cache eviction", "prefix caching", "page table", "memory management LLM", "cache reuse", "cache sharing", "token cache"]},
    {"name": "注意力优化", "icon": "⚡", "keywords": ["flash attention", "paged attention", "attention optimization", "linear attention", "sparse attention", "efficient attention", "multi-head attention", "grouped query attention", "multi-query attention", "gqa", "mqa", "attention kernel"]},
    {"name": "推理系统", "icon": "🚀", "keywords": ["batching", "scheduling", "serving system", "throughput optimization", "vllm", "trt-llm", "tensorrt", "llm serving", "inference engine", "disaggregated serving", "request scheduling", "slo latency", "inference framework", "serving architecture"]},
    {"name": "并行推理", "icon": "🔗", "keywords": ["parallel inference", "distributed inference", "pipeline parallel", "tensor parallel", "model parallelism", "disaggregated prefill", "prefill decode disaggregation", "sequence parallelism", "expert parallelism"]},
    {"name": "算子优化", "icon": "🔧", "keywords": ["operator fusion", "kernel optimization", "cuda kernel", "gpu optimization", "triton kernel", "custom kernel", "fused kernel", "gemm optimization", "kernel generation"]},
]


def _load_categories(categories_cfg=None):
    """从 config 或默认值加载分类规则"""
    if categories_cfg:
        return categories_cfg
    return DEFAULT_CATEGORIES


def classify_paper(paper: Paper, categories_cfg=None) -> tuple[str, str]:
    """根据论文内容匹配分类图标

    Args:
        paper: 论文对象
        categories_cfg: 分类配置列表，None 则使用默认

    Returns:
        (图标, 分类名) 元组
    """
    categories = _load_categories(categories_cfg)
    text = (paper.title + " " + paper.abstract).lower()
    for cat in categories:
        if any(kw in text for kw in cat["keywords"]):
            return cat["icon"], cat["name"]
    return "📄", "通用"


def write_paper_note(
    paper: Paper,
    vault_path: str,
    folder: str = "papers",
    tags: Optional[list[str]] = None,
    categories_cfg=None,
) -> str:
    """将论文写入 Obsidian 笔记

    Args:
        paper: 论文对象（含 summary 字段）
        vault_path: Obsidian Vault 根目录
        folder: 笔记存放子文件夹
        tags: Obsidian 标签列表

    Returns:
        写入的文件路径
    """
    tags = tags or ["论文", "自动生成"]

    # 分类图标
    _icon, category = classify_paper(paper, categories_cfg)

    # 构建目录路径（按中文分类 + 年月分组）
    date_str = paper.published.strftime("%Y-%m")
    output_dir = os.path.join(vault_path, folder, category, date_str)
    os.makedirs(output_dir, exist_ok=True)

    # PDF 下载目录
    pdf_dir = os.path.join(vault_path, folder, category, date_str, "pdf")
    os.makedirs(pdf_dir, exist_ok=True)

    # 生成安全的文件名
    safe_title = _sanitize_filename(paper.title)
    filename = f"{safe_title}.md"
    filepath = os.path.join(output_dir, filename)

    # 如果文件已存在，跳过
    if os.path.exists(filepath):
        print(f"  [跳过] 已存在: {filename}")
        return filepath

    # 下载 PDF
    pdf_filename = f"{safe_title}.pdf"
    pdf_path = os.path.join(pdf_dir, pdf_filename)
    pdf_rel = f"pdf/{pdf_filename}"
    _download_pdf(paper.pdf_url, pdf_path)

    # 构建 Markdown 内容
    content = _build_markdown(paper, tags, pdf_rel, categories_cfg)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"  [写入] {filepath}")
    return filepath


def _download_pdf(pdf_url: str, save_path: str):
    """下载论文 PDF 文件"""
    if os.path.exists(save_path):
        return
    try:
        import urllib.request
        req = urllib.request.Request(pdf_url, headers={
            "User-Agent": "Mozilla/5.0 (paper-reader bot)"
        })
        with urllib.request.urlopen(req, timeout=120) as resp:
            with open(save_path, "wb") as f:
                f.write(resp.read())
        print(f"  [PDF] 已下载: {os.path.basename(save_path)}")
    except Exception as e:
        print(f"  [PDF] 下载失败: {e}")


def _build_markdown(paper: Paper, tags: list[str], pdf_rel: str = "", categories_cfg=None) -> str:
    """构建 Obsidian 格式的 Markdown 内容"""
    # 分类图标
    icon, category = classify_paper(paper, categories_cfg)

    # YAML frontmatter
    tag_str = "\n".join(f"  - {t}" for t in tags)
    author_str = ", ".join(paper.authors[:5])
    if len(paper.authors) > 5:
        author_str += " et al."

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    pub_date = paper.published.strftime("%Y-%m-%d")
    conference = getattr(paper, '_conference', '') or ''
    comment = getattr(paper, 'comment', '') or ''

    frontmatter = f"""---
title: "{paper.title}"
arxiv_id: "{paper.arxiv_id}"
authors: "{author_str}"
published: {pub_date}
categories: [{', '.join(paper.categories[:3])}]
pdf: "{paper.pdf_url}"
pdf_local: "{pdf_rel}"
url: "{paper.abs_url}"
code: "{getattr(paper, '_code_url', '') or ''}"
category: "{category}"
icon: "{icon}"
conference: "{conference}"
status: unread
status_updated: ""
created: {now}
tags:
{tag_str}
---
"""

    # PDF 本地链接
    pdf_link = f"[📥 本地PDF]({pdf_rel})" if pdf_rel else ""

    # 会议标签
    conf_line = f"\n> **会议**: 🏆 {conference}" if conference else ""

    # 正文（标题前加分类图标）
    body = f"""# {icon} {paper.title}

> [!info] 论文状态
> 点击下方链接快速切换 status（需安装 Meta Bind 插件），或在 Properties 面板中手动修改：
> `INPUT[inlineSelect(option(unread, 📬 未读), option(interested, ⭐ 感兴趣), option(rejected, 🗑️ 不感兴趣), option(reading, 📖 阅读中), option(done, ✅ 已读)):status]`

> **领域**: {icon} {category}
> **作者**: {author_str}
> **发表日期**: {pub_date}
> **arXiv**: [{paper.arxiv_id}]({paper.abs_url})
> **PDF**: [在线]({paper.pdf_url}) | {pdf_link}
> **分类**: {', '.join(paper.categories[:3])}{conf_line}

---

"""

    # AI 摘要或原始摘要
    if paper.summary:
        body += paper.summary
    else:
        body += f"## 摘要（Abstract）\n\n{paper.abstract}"

    # 底部
    code_url = getattr(paper, '_code_url', None)
    code_section = ""
    if code_url:
        code_section = f"\n- [代码仓库]({code_url})"

    body += f"""

---

## 快速操作

```paper-actions
```

## 阅读笔记

> 📝 在此添加你的个人笔记和思考...



## 参考链接

- [arXiv 页面]({paper.abs_url})
- [PDF 全文]({paper.pdf_url}){code_section}
"""

    return frontmatter + body


def _sanitize_filename(title: str) -> str:
    """将论文标题转为安全的文件名"""
    # 移除/替换不安全字符
    safe = re.sub(r'[<>:"/\\|?*]', '', title)
    # 截断过长的标题
    if len(safe) > 100:
        safe = safe[:100].rsplit(' ', 1)[0]
    return safe.strip()
