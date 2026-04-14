"""二阶段深度阅读模块 - 对感兴趣的论文进行深度分析

功能：
- 从 GitHub / Papers With Code 搜索并获取代码仓库
- 获取 README 内容和仓库结构
- 生成包含代码分析的详细版笔记追加到已有 Obsidian 笔记
"""

import json
import os
import re
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Optional

import yaml  # type: ignore[import-untyped]

from .llm_client import chat_completion, chat_completion_with_fallback


def _api_get(url: str, timeout: int = 15) -> Optional[dict]:
    """通用 GET 请求，返回 JSON"""
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "paper-reader-bot/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _github_api_get(url: str, token: str = "", timeout: int = 15) -> Optional[dict]:
    """GitHub API GET 请求"""
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "paper-reader-bot/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _get_gh_token() -> str:
    """获取 GitHub token"""
    try:
        import subprocess
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


# ---- 代码仓库搜索 ----

def search_code_repo(title: str, arxiv_id: str, abstract: str = "") -> list[dict]:
    """多渠道搜索论文对应的代码仓库

    Returns:
        [{"url": ..., "source": ..., "stars": ..., "description": ...}, ...]
    """
    results = []
    seen_urls = set()

    # 1. 从 abstract 直接提取 GitHub 链接
    github_pattern = r'https?://github\.com/[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+'
    for url in re.findall(github_pattern, abstract):
        if url not in seen_urls:
            results.append({"url": url, "source": "论文摘要", "stars": -1, "description": ""})
            seen_urls.add(url)

    # 2. 从 arXiv HTML 页面提取代码链接
    arxiv_repos = _search_arxiv_html(arxiv_id)
    for repo in arxiv_repos:
        if repo["url"] not in seen_urls:
            results.append(repo)
            seen_urls.add(repo["url"])

    # 3. Semantic Scholar API
    s2_repos = _search_semantic_scholar(arxiv_id)
    for repo in s2_repos:
        if repo["url"] not in seen_urls:
            results.append(repo)
            seen_urls.add(repo["url"])

    # 4. GitHub 多策略搜索（带验证）
    gh_repos = _search_github(title, arxiv_id=arxiv_id)
    for repo in gh_repos:
        if repo["url"] not in seen_urls:
            results.append(repo)
            seen_urls.add(repo["url"])

    return results


def _search_arxiv_html(arxiv_id: str) -> list[dict]:
    """从 arXiv 页面提取代码仓库链接"""
    results = []
    base_id = re.sub(r'v\d+$', '', arxiv_id)
    url = f"https://arxiv.org/abs/{base_id}"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "paper-reader-bot/1.0",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        # arXiv 页面中 GitHub 链接
        gh_urls = re.findall(r'href="(https?://github\.com/[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)"', html)
        for gh_url in gh_urls:
            # 排除 arXiv 自身的 GitHub
            if "arxiv" not in gh_url.lower():
                results.append({
                    "url": gh_url,
                    "source": "arXiv 页面",
                    "stars": -1,
                    "description": "",
                })
    except Exception:
        pass
    return results


def _search_semantic_scholar(arxiv_id: str) -> list[dict]:
    """从 Semantic Scholar API 获取代码仓库链接"""
    results = []
    base_id = re.sub(r'v\d+$', '', arxiv_id)
    url = f"https://api.semanticscholar.org/graph/v1/paper/ARXIV:{base_id}?fields=externalIds,openAccessPdf"
    try:
        data = _api_get(url, timeout=10)
        if not data:
            return results
        # 检查 openAccessPdf 中是否有 GitHub 链接
        pdf_info = data.get("openAccessPdf") or {}
        pdf_url = pdf_info.get("url", "")
        if "github.com" in pdf_url:
            results.append({
                "url": pdf_url,
                "source": "Semantic Scholar",
                "stars": -1,
                "description": "",
            })
    except Exception:
        pass
    return results


def _search_github(title: str, arxiv_id: str = "") -> list[dict]:
    """通过 GitHub Search API 多策略搜索相关仓库"""
    token = _get_gh_token()
    base_id = re.sub(r'v\d+$', '', arxiv_id) if arxiv_id else ""
    seen_urls: set[str] = set()
    candidates: list[tuple[dict, str]] = []  # (item, query_type)

    # 提取标题中的缩写词/专有名词（EAGLE, AWQ, vLLM, Medusa, PagedAttention 等）
    raw_acronyms = []
    for w in title.split():
        w = w.strip(":,;()[]")
        if not w or len(w) < 2:
            continue
        if w.isupper() and len(w) >= 2:
            raw_acronyms.append(w)  # 全大写: EAGLE, AWQ, LLM
        elif re.match(r'^[a-z]+[A-Z]', w):
            raw_acronyms.append(w)  # 驼峰: vLLM
    acronyms = raw_acronyms

    # 标题中冒号前的第一个词通常是论文的核心名称（如 "Medusa:", "EAGLE:"）
    colon_match = re.match(r'^(\w+)\s*:', title)
    paper_name = colon_match.group(1) if colon_match else ""
    if paper_name and paper_name not in acronyms:
        acronyms.insert(0, paper_name)

    # 构建多个搜索查询（按优先级，标记查询类型）
    queries: list[tuple[str, str]] = []  # (query, type)
    # 策略1: 用 arxiv_id 搜索（最高精度）
    if base_id:
        queries.append((base_id, "arxiv_id"))
    # 策略2: 缩写词 + 领域关键词（如 "EAGLE speculative decoding"）
    domain_words = [w.lower() for w in title.split() if len(w) > 4 and not w.isupper()]
    domain_hint = ' '.join(domain_words[:2]) if domain_words else ""
    for acr in acronyms[:2]:
        if domain_hint:
            queries.append((f"{acr} {domain_hint}", "acronym"))
        queries.append((f"{acr} in:name", "acronym_name"))
    # 策略3: 标题关键词
    stop_words = {'a', 'an', 'the', 'of', 'for', 'and', 'in', 'on', 'to', 'with', 'via', 'by', 'is', 'are', 'its'}
    title_kws = [w for w in title.split() if w.lower() not in stop_words and len(w) > 2]
    if len(title_kws) > 3:
        queries.append((' '.join(title_kws[:5]), "keywords"))

    for q, qtype in queries:
        encoded = urllib.parse.quote(q)
        url = f"https://api.github.com/search/repositories?q={encoded}&sort=stars&per_page=5"
        data = _github_api_get(url, token=token)
        if not data or not data.get("items"):
            continue
        for item in data["items"][:5]:
            html_url = item["html_url"]
            if html_url in seen_urls:
                continue
            seen_urls.add(html_url)
            candidates.append((item, qtype))

    # ML 领域关键词集（用于验证仓库是否与 ML 论文相关）
    ml_indicators = {
        "llm", "transformer", "attention", "inference", "decoding", "model",
        "language", "speculative", "sampling", "quantization", "pruning",
        "lora", "fine-tuning", "serving", "batch", "gpu", "cuda", "pytorch",
        "training", "neural", "deep", "learning", "nlp", "generation",
        "acceleration", "weight", "token", "embedding", "vllm", "llama",
    }

    # 对所有候选仓库进行匹配验证
    results: list[dict] = []
    acronyms_lower = [a.lower() for a in acronyms]

    for item, qtype in candidates:
        desc = (item.get("description") or "").lower()
        name = (item.get("name") or "").lower()
        full_name = (item.get("full_name") or "").lower()
        topics = [t.lower() for t in (item.get("topics") or [])]
        searchable = desc + " " + name + " " + full_name + " " + " ".join(topics)
        is_match = False

        # 信号1：仓库描述/名称中包含 arxiv_id（最可靠）
        if base_id and base_id in searchable:
            is_match = True
        # 信号2：仓库名精确匹配缩写 + 仓库与 ML 领域相关
        if not is_match and any(acr == name for acr in acronyms_lower):
            if any(ind in searchable for ind in ml_indicators):
                is_match = True
        # 信号3：标题关键词 ≥ 3 个匹配（更严格）
        if not is_match:
            title_words = [w.lower() for w in title.split() if len(w) > 3]
            match_count = sum(1 for w in title_words if w in searchable)
            if match_count >= 3:
                is_match = True

        if is_match:
            results.append({
                "url": item["html_url"],
                "source": "GitHub Search",
                "stars": item.get("stargazers_count", 0),
                "description": (item.get("description") or "")[:200],
            })
    return results


# ---- 仓库详情获取 ----

def get_repo_details(repo_url: str) -> dict:
    """获取 GitHub 仓库的详细信息"""
    details = {
        "url": repo_url,
        "full_name": "",
        "stars": 0,
        "forks": 0,
        "language": "",
        "description": "",
        "topics": [],
        "readme": "",
        "tree": [],
        "updated_at": "",
    }

    m = re.match(r'https?://github\.com/([^/]+)/([^/]+)', repo_url)
    if not m:
        return details
    owner, repo = m.group(1), m.group(2).rstrip('.git')
    full_name = f"{owner}/{repo}"
    details["full_name"] = full_name

    token = _get_gh_token()
    api_base = f"https://api.github.com/repos/{full_name}"

    info = _github_api_get(api_base, token=token)
    if info:
        details["stars"] = info.get("stargazers_count", 0)
        details["forks"] = info.get("forks_count", 0)
        details["language"] = info.get("language", "")
        details["description"] = (info.get("description") or "")[:500]
        details["topics"] = info.get("topics", [])[:10]
        details["updated_at"] = info.get("updated_at", "")

    readme = _github_api_get(f"{api_base}/readme", token=token)
    if readme and readme.get("content"):
        import base64
        try:
            readme_text = base64.b64decode(readme["content"]).decode("utf-8", errors="replace")
            if len(readme_text) > 5000:
                readme_text = readme_text[:5000] + "\n\n... (README 过长，已截断)"
            details["readme"] = readme_text
        except Exception:
            pass

    tree = _github_api_get(f"{api_base}/git/trees/HEAD?recursive=0", token=token)
    if not tree:
        tree = _github_api_get(f"{api_base}/git/trees/main?recursive=0", token=token)
    if tree and tree.get("tree"):
        details["tree"] = [
            {"path": item["path"], "type": item["type"]}
            for item in tree["tree"][:50]
        ]

    return details


# ---- 深度笔记生成 ----

def generate_deep_note(
    title: str,
    arxiv_id: str,
    abstract: str,
    repos: list[dict],
    repo_details: Optional[dict],
    ai_analysis: Optional[str],
) -> str:
    """生成深度阅读的 Markdown 内容"""
    sections = []
    sections.append("\n\n---\n\n## 🔬 二阶段深度分析\n")
    sections.append(f"> 深度分析时间: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

    sections.append("### 📦 相关代码仓库\n")
    if repos:
        sections.append("| 仓库 | 来源 | Stars | 说明 |")
        sections.append("|------|------|-------|------|")
        for r in repos:
            stars = f"⭐ {r['stars']}" if r['stars'] >= 0 else "N/A"
            desc = r['description'][:80] if r['description'] else "-"
            sections.append(f"| [{r['url'].split('github.com/')[-1] if 'github.com' in r['url'] else r['url'][:40]}]({r['url']}) | {r['source']} | {stars} | {desc} |")
    else:
        sections.append("> ⚠️ 未找到相关代码仓库\n")

    if repo_details and repo_details.get("full_name"):
        rd = repo_details
        sections.append(f"\n### 🏠 主仓库详情: [{rd['full_name']}]({rd['url']})\n")
        sections.append(f"- **语言**: {rd['language'] or 'N/A'}")
        sections.append(f"- **Stars**: ⭐ {rd['stars']} | **Forks**: 🍴 {rd['forks']}")
        if rd['topics']:
            sections.append(f"- **Topics**: {', '.join(f'`{t}`' for t in rd['topics'])}")
        if rd['updated_at']:
            sections.append(f"- **最后更新**: {rd['updated_at'][:10]}")
        sections.append(f"- **描述**: {rd['description'] or 'N/A'}\n")

        if rd['tree']:
            sections.append("#### 📁 仓库结构\n")
            sections.append("```")
            for item in rd['tree']:
                prefix = "📁 " if item['type'] == 'tree' else "📄 "
                sections.append(f"{prefix}{item['path']}")
            sections.append("```\n")

        if rd['readme']:
            sections.append("#### 📖 README 内容\n")
            sections.append("<details>")
            sections.append("<summary>点击展开 README</summary>\n")
            sections.append(rd['readme'])
            sections.append("\n</details>\n")

    if ai_analysis:
        sections.append(f"\n### 🤖 AI 深度分析\n")
        sections.append(ai_analysis)

    return "\n".join(sections)


DEEP_ANALYSIS_PROMPT = """你是一位专注于大模型推理优化的研究工程师。
我给你一篇论文的信息和它对应的代码仓库信息，请做深入分析。

要求用中文撰写，严格按以下格式输出：

## 代码实现分析
（根据仓库结构和 README，分析代码的实现架构、主要模块、依赖项）

## 复现指南
（根据 README，列出复现步骤：环境配置、数据准备、训练/推理命令等）

## 代码质量评估
（从代码规范性、文档完善度、测试覆盖、社区活跃度等角度评估）

## 可借鉴的实现细节
（列出对我的研究有价值的具体代码实现技巧或设计模式）

## 集成建议
（如果要将这个方法集成到自己的推理系统中，需要注意什么？需要修改哪些部分？）"""


def ai_deep_analysis(
    title: str,
    abstract: str,
    repo_details: dict,
    provider: str = "codex",
    base_url: str = "",
    api_key: str = "",
    model: str = "",
    codex_cli_path: str = "",
    config: dict | None = None,
) -> Optional[str]:
    """使用统一 LLM 客户端对代码仓库进行深度分析"""
    readme_excerpt = repo_details.get("readme", "")[:3000]
    tree_str = "\n".join(
        f"{'📁' if item['type'] == 'tree' else '📄'} {item['path']}"
        for item in repo_details.get("tree", [])[:30]
    )

    user_prompt = f"""论文标题: {title}

论文摘要:
{abstract[:1000]}

代码仓库: {repo_details.get('url', '')}
语言: {repo_details.get('language', 'N/A')}
Stars: {repo_details.get('stars', 0)}

目录结构:
{tree_str}

README 内容:
{readme_excerpt}"""

    messages = [
        {"role": "system", "content": DEEP_ANALYSIS_PROMPT},
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


# ---- 扫描感兴趣的论文 ----

def find_interested_papers(vault_path: str, folder: str = "papers") -> list[dict]:
    """扫描 Obsidian vault 中标记为感兴趣的论文"""
    interested = []
    papers_dir = os.path.join(vault_path, folder)
    if not os.path.exists(papers_dir):
        return interested

    for root, dirs, files in os.walk(papers_dir):
        for f in files:
            if not f.endswith(".md"):
                continue
            filepath = os.path.join(root, f)
            try:
                with open(filepath, "r", encoding="utf-8") as fh:
                    content = fh.read()
                if not content.startswith("---"):
                    continue
                end = content.find("---", 3)
                if end == -1:
                    continue
                fm_text = content[3:end]
                fm = yaml.safe_load(fm_text)
                if not fm:
                    continue
                # 兼容新旧字段：优先读 status，回退到 interested
                status = fm.get("status", "")
                if not status:
                    status = "interested" if fm.get("interested") is True else "unread"
                if status in ("interested", "reading"):
                    has_deep = "## 🔬 二阶段深度分析" in content
                    interested.append({
                        "filepath": filepath,
                        "title": fm.get("title", ""),
                        "arxiv_id": fm.get("arxiv_id", ""),
                        "code": fm.get("code", ""),
                        "category": fm.get("category", ""),
                        "icon": fm.get("icon", ""),
                        "status": status,
                        "status_updated": fm.get("status_updated", fm.get("interested_at", "")),
                        "has_deep": has_deep,
                    })
            except Exception:
                continue

    interested.sort(key=lambda x: x.get("status_updated", ""), reverse=True)
    return interested


def append_deep_note(filepath: str, deep_content: str):
    """将深度分析内容追加到已有笔记（已有则替换，防止重复）"""
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    deep_marker = "## 🔬 二阶段深度分析"
    if deep_marker in content:
        # 已有深度分析：替换旧内容
        idx = content.index(deep_marker)
        # 找到下一个同级 ## 标题作为结束边界
        next_h2 = re.search(r"\n## (?!🔬)", content[idx + 1:])
        end_idx = (idx + 1 + next_h2.start()) if next_h2 else len(content)
        content = content[:idx] + deep_content.strip() + "\n\n" + content[end_idx:]
    else:
        marker = "## 阅读笔记"
        if marker in content:
            content = content.replace(
                f"---\n\n{marker}",
                f"{deep_content}\n\n---\n\n{marker}",
            )
        else:
            content += deep_content

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)


def post_process_deep_analysis(filepath: str):
    """深度分析写入后的自动后处理：更新 frontmatter、标签、重构页面结构"""
    AI_SECTIONS = {
        '一句话总结', '原始摘要（Abstract）', '摘要翻译', '方法概览图',
        '研究背景', '研究动机', '核心方法', '主要结果', '结论',
        '与我的研究相关性', '代码与复现', '关键术语',
    }
    KEEP_SECTIONS = {'快速操作', '阅读笔记', '参考链接'}

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    original_content = content

    if not content.startswith("---"):
        return
    end = content.find("---", 3)
    if end == -1:
        return

    # --- 1. 更新 frontmatter ---
    fm_str = content[3:end]
    new_fm = fm_str
    # deep_analysis 字段
    if "deep_analysis:" in new_fm:
        new_fm = re.sub(r"deep_analysis:\s*\S+", "deep_analysis: true", new_fm)
    else:
        new_fm = new_fm.rstrip("\n") + "\ndeep_analysis: true\n"
    # 确保有深度分析标签
    if "深度分析" not in new_fm:
        new_fm = new_fm.rstrip("\n") + "\n  - 深度分析\n"
    content = "---" + new_fm + content[end:]
    # 重新定位 end
    end = content.find("---", 3)

    # --- 2. 更新正文中的深度分析可视化标签 ---
    content = content.replace(
        "> **深度分析**: ❌ 未分析",
        "> **深度分析**: ✅ 已完成",
    )
    content = content.replace(
        "> - **深度分析**: ❌ 未分析",
        "> - **深度分析**: ✅ 已完成",
    )

    # --- 3. 重构页面：深度分析提前，AI 摘要折叠 ---
    if "[!abstract]- 📋 AI 自动摘要" in content or "## 🔬 二阶段深度分析" not in content:
        # 已重构过或无深度分析段落，仅在有变更时写入
        if content != original_content:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)
        return

    body_start = end + 3
    body = content[body_start:]

    first_h2 = body.find("\n## ")
    if first_h2 == -1:
        if content != original_content:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)
        return

    header_part = body[:first_h2]
    rest = body[first_h2:]

    section_pattern = re.compile(r"^## (.+)$", re.MULTILINE)
    matches = list(section_pattern.finditer(rest))

    sections = []
    for i, m in enumerate(matches):
        title = m.group(1).strip()
        start = m.start()
        end_pos = matches[i + 1].start() if i + 1 < len(matches) else len(rest)
        sections.append((title, rest[start:end_pos]))

    deep_section = None
    ai_sections = []
    keep_sections = []

    for title, text in sections:
        if "🔬 二阶段深度分析" in title:
            deep_section = text
        elif title in AI_SECTIONS:
            ai_sections.append(text)
        elif title in KEEP_SECTIONS:
            keep_sections.append(text)
        else:
            # 未知段落保留原位，不折叠进 AI 摘要
            keep_sections.append(text)

    if not deep_section:
        if content != original_content:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)
        return

    new_body = header_part.rstrip("\n") + "\n\n"
    new_body += deep_section.rstrip("\n") + "\n"
    if ai_sections:
        new_body += "\n---\n\n"
        callout_lines = ["> [!abstract]- 📋 AI 自动摘要（点击展开）", ">"]
        for sec in ai_sections:
            for line in sec.split("\n"):
                callout_lines.append("> " + line if line.strip() else ">")
        new_body += "\n".join(callout_lines) + "\n"
    new_body += "\n---\n"
    for sec in keep_sections:
        new_body += "\n" + sec.strip("\n") + "\n"

    # 清理可能产生的双重分隔线
    while "\n---\n\n---\n" in new_body:
        new_body = new_body.replace("\n---\n\n---\n", "\n---\n")

    content = content[:body_start] + new_body

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
