"""二次检查模块 - 对已生成的论文笔记进行质量校验与自动修复

检查项：
1. frontmatter 完整性（必填字段是否齐全）
2. AI 摘要是否生成（检测 fallback 标记）
3. 必要段落是否完整（11 个预期段落）
4. Mermaid 流程图是否存在
5. 分类是否与内容匹配
6. 段落内容是否过短（占位 / 无意义）

修复策略：
- fallback 摘要 → 重新调用 AI 生成
- 段落缺失 / 过短 → 重新调用 AI 生成
- 分类不匹配 → 打印警告（不自动移动文件，避免破坏链接）
"""

import os
import re
from dataclasses import dataclass, field
from typing import Optional

import yaml  # type: ignore[import-untyped]

from .obsidian_writer import DEFAULT_CATEGORIES


# 期望的完整 AI 摘要必须包含的段落标题
EXPECTED_SECTIONS = [
    "一句话总结",
    "原始摘要",
    "摘要翻译",
    "方法概览图",
    "研究背景",
    "研究动机",
    "核心方法",
    "主要结果",
    "结论",
    "与我的研究相关性",
    "代码与复现",
    "关键术语",
]

# frontmatter 必填字段
REQUIRED_FM_FIELDS = [
    "title", "arxiv_id", "authors", "published", "categories",
    "pdf", "url", "category", "icon", "tags", "status",
]

# 段落最低有效字符数（低于此视为占位内容）
MIN_SECTION_LENGTH = 15


@dataclass
class CheckResult:
    """单篇论文的检查结果"""
    filepath: str
    title: str
    arxiv_id: str
    issues: list[str] = field(default_factory=list)
    needs_regenerate: bool = False  # 需要重新生成 AI 摘要
    category_mismatch: str = ""     # 建议的分类（空 = 匹配正确）

    @property
    def passed(self) -> bool:
        return len(self.issues) == 0


def check_paper_note(filepath: str) -> CheckResult:
    """对单篇论文笔记执行全部检查

    Returns:
        CheckResult 包含所有发现的问题
    """
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # 解析 frontmatter
    fm = {}
    body = content
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            fm = yaml.safe_load(content[3:end]) or {}
            body = content[end + 3:]

    result = CheckResult(
        filepath=filepath,
        title=fm.get("title", os.path.basename(filepath)),
        arxiv_id=fm.get("arxiv_id", ""),
    )

    # ---- 检查 1: frontmatter 完整性 ----
    for field_name in REQUIRED_FM_FIELDS:
        if field_name not in fm or fm[field_name] is None or fm[field_name] == "":
            # categories 和 tags 可以是空列表
            if field_name in ("categories", "tags") and isinstance(fm.get(field_name), list):
                continue
            result.issues.append(f"frontmatter 缺少字段: {field_name}")

    # ---- 检查 2: 是否为 fallback 摘要 ----
    if "此笔记由系统自动生成（未经 AI 总结）" in content:
        result.issues.append("使用了 fallback 摘要（未经 AI 总结）")
        result.needs_regenerate = True
        return result  # fallback 无需检查段落

    # ---- 检查 3: 必要段落是否存在 ----
    missing_sections = []
    for section in EXPECTED_SECTIONS:
        # 匹配 ## 段落标题（允许标题后有额外文字）
        pattern = rf'^##\s+.*{re.escape(section)}'
        if not re.search(pattern, body, re.MULTILINE):
            missing_sections.append(section)

    if missing_sections:
        result.issues.append(f"缺少段落: {', '.join(missing_sections)}")
        # 缺少 3 个以上核心段落，建议重新生成
        core_missing = [
            s for s in missing_sections
            if s in ("摘要翻译", "核心方法", "主要结果", "研究背景", "研究动机")
        ]
        if len(core_missing) >= 2:
            result.needs_regenerate = True

    # ---- 检查 4: Mermaid 流程图 ----
    if "```mermaid" not in body:
        result.issues.append("缺少 Mermaid 方法概览图")

    # ---- 检查 5: 段落内容质量（过短检测）----
    short_sections = _find_short_sections(body)
    if short_sections:
        result.issues.append(f"以下段落内容过短: {', '.join(short_sections)}")
        # 多个核心段落过短，建议重新生成
        core_short = [
            s for s in short_sections
            if s in ("摘要翻译", "核心方法", "主要结果", "研究背景")
        ]
        if len(core_short) >= 2:
            result.needs_regenerate = True

    # ---- 检查 6: 分类匹配 ----
    current_category = fm.get("category", "")
    if current_category and fm.get("title"):
        suggested = _suggest_category(fm["title"], body)
        if suggested and suggested != current_category:
            result.category_mismatch = suggested
            result.issues.append(
                f"分类可能不匹配: 当前={current_category}, 建议={suggested}"
            )

    return result


def _find_short_sections(body: str) -> list[str]:
    """查找内容过短的段落"""
    short = []
    # 按 ## 分割段落
    parts = re.split(r'^(##\s+.+)$', body, flags=re.MULTILINE)
    # parts 交替为：段间文本, 标题, 内容, 标题, 内容, ...
    i = 1
    while i < len(parts) - 1:
        header = parts[i].strip()
        content = parts[i + 1].strip()
        # 提取段落名（去掉 ## 和可能的图标）
        section_name = re.sub(r'^##\s+', '', header)
        # 跳过不需要检查的段落
        skip = ("阅读笔记", "参考链接", "快速操作", "方法概览图")
        if any(s in section_name for s in skip):
            i += 2
            continue
        # 计算有效内容长度（去掉 markdown 语法）
        clean = re.sub(r'[#*`>\-|]', '', content)
        clean = clean.strip()
        if len(clean) < MIN_SECTION_LENGTH:
            # 归类到最接近的 EXPECTED_SECTIONS 名称
            for expected in EXPECTED_SECTIONS:
                if expected in section_name:
                    short.append(expected)
                    break
        i += 2
    return short


def _suggest_category(title: str, body: str) -> str:
    """根据内容重新推断分类"""
    text = (title + " " + body[:2000]).lower()
    best_score = 0
    best_category = ""
    for cat in DEFAULT_CATEGORIES:
        score = sum(1 for kw in cat["keywords"] if kw in text)
        if score > best_score:
            best_score = score
            best_category = cat["name"]
    return best_category if best_score > 0 else ""


def check_vault_papers(
    vault_path: str,
    folder: str = "papers",
    only_recent: int = 0,
) -> list[CheckResult]:
    """扫描 vault 中所有论文笔记并执行检查

    Args:
        vault_path: Obsidian vault 路径
        folder: 论文文件夹
        only_recent: 只检查最近 N 篇（0 = 全部）

    Returns:
        CheckResult 列表（只返回有问题的）
    """
    papers_dir = os.path.join(vault_path, folder)
    if not os.path.exists(papers_dir):
        return []

    all_files = []
    for root, _dirs, files in os.walk(papers_dir):
        for f in files:
            if f.endswith(".md") and not f.startswith("_"):
                all_files.append(os.path.join(root, f))

    # 按修改时间倒序排列
    all_files.sort(key=lambda p: os.path.getmtime(p), reverse=True)

    if only_recent > 0:
        all_files = all_files[:only_recent]

    results = []
    for filepath in all_files:
        try:
            result = check_paper_note(filepath)
            if not result.passed:
                results.append(result)
        except Exception:
            continue

    return results


def regenerate_summary(
    filepath: str,
    config: dict,
) -> bool:
    """对检查不通过的论文重新生成 AI 摘要

    读取 frontmatter 重建 Paper 对象，调用 AI 重新生成摘要并覆写正文部分。

    Returns:
        是否成功
    """
    from .arxiv_fetcher import Paper
    from .summarizer import summarize_paper
    from .obsidian_writer import classify_paper
    from datetime import datetime, timezone

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # 解析 frontmatter
    if not content.startswith("---"):
        return False
    end = content.find("---", 3)
    if end == -1:
        return False
    fm = yaml.safe_load(content[3:end]) or {}

    # 从 frontmatter 重建 Paper
    paper = Paper(
        arxiv_id=fm.get("arxiv_id", ""),
        title=fm.get("title", ""),
        authors=[a.strip() for a in fm.get("authors", "").split(",")],
        abstract="",  # 从正文中提取
        categories=fm.get("categories", []),
        published=datetime.now(timezone.utc),
        updated=datetime.now(timezone.utc),
        pdf_url=fm.get("pdf", ""),
        abs_url=fm.get("url", ""),
        primary_category=fm.get("categories", [""])[0] if fm.get("categories") else "",
    )

    # 从正文提取原始摘要
    body = content[end + 3:]
    abstract_match = re.search(
        r'## 原始摘要[^\n]*\n(.*?)(?=\n## |\n---)',
        body,
        re.DOTALL,
    )
    if abstract_match:
        paper.abstract = abstract_match.group(1).strip()
    else:
        # 尝试从 "Abstract" 段落提取
        abstract_match = re.search(
            r'## 摘要[^\n]*\n(.*?)(?=\n## |\n---)',
            body,
            re.DOTALL,
        )
        if abstract_match:
            paper.abstract = abstract_match.group(1).strip()

    if not paper.abstract:
        print(f"    ⚠️ 无法提取原始摘要，跳过: {os.path.basename(filepath)}")
        return False

    # 设置代码信息
    code_url = fm.get("code", "")
    if code_url:
        paper._code_url = code_url

    # 调用 AI 重新生成
    ai_cfg = config.get("ai", {})
    new_summary = summarize_paper(
        paper,
        provider=ai_cfg.get("provider", "codex"),
        base_url=ai_cfg.get("base_url", ""),
        api_key=ai_cfg.get("api_key", ""),
        model=ai_cfg.get("model", ""),
        language=ai_cfg.get("language", "中文"),
        codex_cli_path=ai_cfg.get("codex_cli_path", ""),
        config=config,
    )

    if not new_summary:
        return False

    # 替换正文中 "---" 分隔线后到 "## 阅读笔记" 之间的内容
    # 保留 frontmatter + 头部元信息 + 尾部阅读笔记/参考链接
    header_end = body.find("\n---\n")
    if header_end == -1:
        header_end = 0
    else:
        header_end += len("\n---\n")

    header_part = body[:header_end]

    # 尾部保留（阅读笔记 + 参考链接 + 深度分析）
    tail_part = ""
    tail_markers = ["## 阅读笔记", "## 🔬 二阶段深度分析"]
    for marker in tail_markers:
        tail_idx = body.find(marker)
        if tail_idx != -1:
            # 包含 marker 前面的 "---" 分隔线
            pre_sep = body.rfind("\n---\n", 0, tail_idx)
            if pre_sep != -1:
                tail_part = body[pre_sep:]
            else:
                tail_part = "\n\n---\n\n" + body[tail_idx:]
            break

    if not tail_part:
        tail_part = """

---

## 阅读笔记

> 📝 在此添加你的个人笔记和思考...



## 参考链接

- [arXiv 页面](""" + fm.get("url", "") + """)
- [PDF 全文](""" + fm.get("pdf", "") + ")"

    new_body = header_part + "\n" + new_summary + tail_part
    new_content = content[:end + 3] + new_body

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(new_content)

    return True


def print_check_report(results: list[CheckResult]):
    """打印检查报告"""
    if not results:
        print("  ✅ 所有论文笔记检查通过，无问题")
        return

    regen_count = sum(1 for r in results if r.needs_regenerate)
    warn_count = len(results) - regen_count

    print(f"  发现 {len(results)} 篇论文有问题 "
          f"(🔴 需重新生成: {regen_count}, 🟡 警告: {warn_count})\n")

    for i, r in enumerate(results, 1):
        status = "🔴" if r.needs_regenerate else "🟡"
        print(f"  {i}. {status} {r.title[:55]}")
        print(f"     {r.arxiv_id}")
        for issue in r.issues:
            print(f"     • {issue}")
        print()
