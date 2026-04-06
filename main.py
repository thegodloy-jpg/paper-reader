"""论文自动阅读系统 v2 - 主入口（Codex / LiteLLM 多后端版本）

支持通过 Codex CLI, LiteLLM, 或原始 API 调用模型。

用法:
    python -m paper_reader_v2.main scan                  # 扫描新论文（默认模式，含二次检查）
    python -m paper_reader_v2.main scan --no-ai          # 跳过 AI 摘要
    python -m paper_reader_v2.main scan --no-check       # 跳过二次检查
    python -m paper_reader_v2.main scan --dry-run        # 仅抓取，不写入
    python -m paper_reader_v2.main check                 # 检查全部已有论文笔记质量
    python -m paper_reader_v2.main check --fix           # 检查并自动修复
    python -m paper_reader_v2.main check --recent 10     # 只检查最近 10 篇
    python -m paper_reader_v2.main deep                  # 深度分析所有收藏论文
    python -m paper_reader_v2.main deep --paper 2401.12345  # 分析指定论文
    python -m paper_reader_v2.main deep --force          # 重新分析（即使已有深度笔记）
    python -m paper_reader_v2.main list                  # 查看收藏论文
    python -m paper_reader_v2.main list --all            # 查看所有论文
    python -m paper_reader_v2.main cleanup               # 清理 rejected 论文（预览）
    python -m paper_reader_v2.main cleanup --confirm     # 确认删除 rejected 论文
    python -m paper_reader_v2.main cleanup --days 7      # 仅清理 7 天前的论文
    python -m paper_reader_v2.main sync-history          # 同步 vault 到 history.json
    python -m paper_reader_v2.main migrate               # 迁移旧 interested 字段为 status
    python -m paper_reader_v2.main fix                   # 修复 fallback 摘要论文
    python -m paper_reader_v2.main fix --limit 5         # 只修复 5 篇
"""

import argparse
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Windows GBK 编码无法输出 emoji，强制 UTF-8
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import yaml  # type: ignore[import-untyped]

from .arxiv_fetcher import fetch_papers
from .semantic_scholar_fetcher import fetch_papers_s2, fetch_conference_papers_s2
from .paperswithcode_fetcher import fetch_papers_pwc
from .summarizer import summarize_paper, generate_fallback_summary
from .obsidian_writer import write_paper_note, classify_paper
from .filters import filter_duplicates, filter_by_code, mark_processed, sort_by_priority, sync_vault_to_history, log_activity
from .interest_tracker import extract_interest_keywords
from .deep_reader import (
    search_code_repo,
    get_repo_details,
    generate_deep_note,
    ai_deep_analysis,
    find_interested_papers,
    append_deep_note,
)
from .post_check import (
    check_vault_papers,
    print_check_report,
    regenerate_summary,
)


DEFAULT_CONFIG = Path(__file__).parent / "config.yaml"


def load_config(config_path: str) -> dict:
    """加载 YAML 配置文件"""
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# =============================================================================
# scan 模式：扫描新论文
# =============================================================================

def cmd_scan(config: dict, no_ai: bool = False, dry_run: bool = False, no_check: bool = False):
    """执行扫描主流程"""
    arxiv_cfg = config.get("arxiv", {})
    ai_cfg = config.get("ai", {})
    output_cfg = config.get("output", {})
    vault_path = config.get("obsidian_vault", "")
    categories_cfg = config.get("categories", None)

    if not vault_path and not dry_run:
        print("❌ 错误：请在 config.yaml 中配置 obsidian_vault 路径")
        sys.exit(1)

    # --- 步骤 1: 抓取论文 ---
    print("=" * 60)
    print("📚 论文自动阅读系统 v2 — 扫描模式")
    print(f"   🤖 AI 后端: {ai_cfg.get('provider', 'codex')} / {ai_cfg.get('model', 'N/A')}")
    print("=" * 60)
    print(f"\n🔍 正在从 arXiv 搜索论文...")
    print(f"   关键词: {', '.join(arxiv_cfg.get('keywords', []))}")
    print(f"   分类: {', '.join(arxiv_cfg.get('categories', []))}")

    papers = fetch_papers(
        keywords=arxiv_cfg.get("keywords", []),
        categories=arxiv_cfg.get("categories", []),
        max_results=arxiv_cfg.get("max_papers", 5),
        days_back=arxiv_cfg.get("days_back", 3),
    )

    # --- 步骤 1.1: 兴趣驱动关键词扩展 ---
    if vault_path:
        interest_kws = extract_interest_keywords(
            vault_path,
            folder="papers",
            existing_keywords=arxiv_cfg.get("keywords", []),
            max_keywords=5,
        )
        if interest_kws:
            print(f"\n🧠 根据你感兴趣的论文，自动发现新关键词:")
            for kw in interest_kws:
                print(f"   + {kw}")
            extra_papers = fetch_papers(
                keywords=interest_kws,
                categories=arxiv_cfg.get("categories", []),
                max_results=max(5, arxiv_cfg.get("max_papers", 5) // 2),
                days_back=arxiv_cfg.get("days_back", 3),
            )
            if extra_papers:
                # 去重合并
                existing_ids = {p.arxiv_id for p in papers}
                new_count = 0
                for p in extra_papers:
                    if p.arxiv_id not in existing_ids:
                        papers.append(p)
                        existing_ids.add(p.arxiv_id)
                        new_count += 1
                if new_count:
                    print(f"   📥 额外找到 {new_count} 篇相关论文")

    # --- 步骤 1.2: 多源论文抓取（Semantic Scholar + Papers With Code） ---
    sources_cfg = config.get("sources", {})
    all_keywords = arxiv_cfg.get("keywords", [])
    existing_ids = {p.arxiv_id for p in papers}

    if sources_cfg.get("semantic_scholar", {}).get("enabled", False):
        print(f"\n🔬 正在从 Semantic Scholar 搜索论文...")
        try:
            s2_papers = fetch_papers_s2(
                keywords=all_keywords,
                max_results=sources_cfg["semantic_scholar"].get("max_papers", 20),
                days_back=arxiv_cfg.get("days_back", 30),
                min_citation_count=sources_cfg["semantic_scholar"].get("min_citations", 0),
            )
            s2_new = 0
            for p in s2_papers:
                if p.arxiv_id not in existing_ids:
                    papers.append(p)
                    existing_ids.add(p.arxiv_id)
                    s2_new += 1
            print(f"   📥 Semantic Scholar 新增 {s2_new} 篇论文")
        except Exception as e:
            print(f"   ⚠️ Semantic Scholar 查询失败: {e}")

    if sources_cfg.get("paperswithcode", {}).get("enabled", False):
        print(f"\n💻 正在从 Papers With Code 搜索论文...")
        try:
            pwc_papers = fetch_papers_pwc(
                keywords=all_keywords,
                max_results=sources_cfg["paperswithcode"].get("max_papers", 20),
                days_back=arxiv_cfg.get("days_back", 30),
            )
            pwc_new = 0
            for p in pwc_papers:
                if p.arxiv_id not in existing_ids:
                    papers.append(p)
                    existing_ids.add(p.arxiv_id)
                    pwc_new += 1
            print(f"   📥 Papers With Code 新增 {pwc_new} 篇论文")
        except Exception as e:
            print(f"   ⚠️ Papers With Code 查询失败: {e}")

    # --- 步骤 1.3: 顶会论文搜索 ---
    conf_cfg = sources_cfg.get("conferences", {})
    if conf_cfg.get("enabled", False):
        conf_list = conf_cfg.get("venues", [])
        conf_year = conf_cfg.get("year", None)
        if conf_list:
            print(f"\n🏆 正在从 Semantic Scholar 搜索顶会论文...")
            print(f"   会议: {', '.join(conf_list)}")
            try:
                conf_papers = fetch_conference_papers_s2(
                    keywords=all_keywords,
                    conferences=conf_list,
                    max_results=conf_cfg.get("max_papers", 20),
                    year=conf_year,
                )
                conf_new = 0
                for p in conf_papers:
                    if p.arxiv_id not in existing_ids:
                        papers.append(p)
                        existing_ids.add(p.arxiv_id)
                        conf_new += 1
                print(f"   📥 顶会论文新增 {conf_new} 篇")
            except Exception as e:
                print(f"   ⚠️ 顶会搜索失败: {e}")

    if not papers:
        print("\n📭 没有找到最近的新论文，稍后再试吧")
        return

    print(f"\n📄 找到 {len(papers)} 篇论文")

    # --- 步骤 1.5: 去重 ---
    if arxiv_cfg.get("skip_duplicates", True):
        papers = filter_duplicates(papers)
        if not papers:
            print("\n📭 所有论文都已处理过，没有新论文")
            return

    # --- 步骤 1.6: 代码仓库检测与过滤 ---
    if arxiv_cfg.get("prefer_with_code", True):
        papers = filter_by_code(papers, prefer_with_code=True, categories_cfg=categories_cfg)
        if not papers:
            print("\n📭 过滤后没有符合条件的论文")
            return

    # --- 步骤 1.7: 优先级排序（含反馈学习） ---
    feedback_profile = None
    if vault_path:
        from .interest_tracker import build_feedback_profile
        feedback_profile = build_feedback_profile(vault_path, output_cfg.get("folder", "papers"))
    papers = sort_by_priority(papers, feedback_profile=feedback_profile)

    print(f"\n📋 最终保留 {len(papers)} 篇论文:\n")
    for i, p in enumerate(papers, 1):
        code_tag = " 📦" if getattr(p, '_code_url', None) else ""
        conf_tag = f" 🏆{getattr(p, '_conference', '')}" if getattr(p, '_conference', None) else ""
        icon, category = classify_paper(p, categories_cfg)
        print(f"  {i}. {icon} [{category}] {p.title}{code_tag}{conf_tag}")
        print(f"     {p.published.strftime('%Y-%m-%d')} | {p.arxiv_id}")

    # --- 步骤 2: AI 摘要 ---
    if not no_ai:
        provider = ai_cfg.get("provider", "codex")
        model = ai_cfg.get("model", "")
        print(f"\n🤖 正在生成 AI 摘要 (provider: {provider}, 模型: {model})...\n")
        consecutive_failures = 0
        for i, paper in enumerate(papers, 1):
            print(f"  [{i}/{len(papers)}] {paper.title[:60]}...")
            if consecutive_failures >= 3:
                print(f"  ⚠️ 连续 {consecutive_failures} 次 AI 调用失败，后续论文使用模板摘要")
                paper.summary = generate_fallback_summary(paper)
                continue
            try:
                summary = summarize_paper(
                    paper,
                    provider=provider,
                    base_url=ai_cfg.get("base_url", ""),
                    api_key=ai_cfg.get("api_key", ""),
                    model=model,
                    language=ai_cfg.get("language", "中文"),
                    codex_cli_path=ai_cfg.get("codex_cli_path", ""),
                    config=config,
                )
            except Exception as e:
                print(f"  ❌ 异常: {e}")
                summary = None
            if summary:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
            paper.summary = summary or generate_fallback_summary(paper)
            # 论文间冷却延迟，避免触发 API 429 限流
            if i < len(papers):
                time.sleep(8)
    else:
        print("\n⏭️  跳过 AI 摘要生成")
        for paper in papers:
            paper.summary = generate_fallback_summary(paper)

    # --- 步骤 3: 写入 Obsidian ---
    if dry_run:
        print("\n🏁 Dry-run 模式，不写入文件")
        for paper in papers:
            print(f"\n{'─' * 40}")
            print(f"📄 {paper.title}")
            print(paper.summary[:300] + "..." if len(paper.summary) > 300 else paper.summary)
        return

    print(f"\n📝 正在写入 Obsidian 笔记 -> {vault_path}\n")
    written = 0
    for paper in papers:
        path = write_paper_note(
            paper,
            vault_path=vault_path,
            folder=output_cfg.get("folder", "papers"),
            tags=output_cfg.get("tags", ["论文", "自动生成"]),
            categories_cfg=categories_cfg,
        )
        if path:
            written += 1
            mark_processed([paper])

    print(f"\n✅ 完成！共写入 {written} 篇论文笔记")
    print(f"   笔记位置: {os.path.join(vault_path, output_cfg.get('folder', 'papers'))}")
    print(f"   打开 Obsidian 即可阅读 📖")
    log_activity("scan", {"papers_found": len(papers), "papers_written": written})

    # --- 步骤 4: 二次检查（默认执行） ---
    if not no_check and written > 0:
        print(f"\n🔍 二次检查：验证刚写入的 {written} 篇笔记质量...")
        results = check_vault_papers(
            vault_path, output_cfg.get("folder", "papers"),
            only_recent=written,
        )
        print_check_report(results)
        # 自动修复需要重新生成的论文
        regen = [r for r in results if r.needs_regenerate]
        if regen and not no_ai:
            print(f"\n🔧 自动修复：重新生成 {len(regen)} 篇不合格摘要...\n")
            fixed = 0
            for i, r in enumerate(regen, 1):
                print(f"  [{i}/{len(regen)}] {r.title[:55]}...")
                try:
                    ok = regenerate_summary(r.filepath, config)
                    if ok:
                        fixed += 1
                        print(f"    ✅ 重新生成成功")
                    else:
                        print(f"    ❌ 重新生成失败")
                except Exception as e:
                    print(f"    ❌ 异常: {e}")
                if i < len(regen):
                    time.sleep(8)
            print(f"\n  🔧 修复完成: {fixed}/{len(regen)} 篇")


# =============================================================================
# deep 模式：深度分析收藏论文
# =============================================================================

def cmd_deep(config: dict, paper_id: str = "", force: bool = False):
    """对收藏论文进行二阶段深度分析"""
    ai_cfg = config.get("ai", {})
    output_cfg = config.get("output", {})
    vault_path = config.get("obsidian_vault", "")

    if not vault_path:
        print("❌ 错误：请在 config.yaml 中配置 obsidian_vault 路径")
        sys.exit(1)

    print("=" * 60)
    print("🔬 论文自动阅读系统 v2 — 深度分析模式")
    print(f"   🤖 AI 后端: {ai_cfg.get('provider', 'codex')} / {ai_cfg.get('model', 'N/A')}")
    print("=" * 60)

    folder = output_cfg.get("folder", "papers")

    # 获取待分析论文列表
    if paper_id:
        papers = find_interested_papers(vault_path, folder)
        all_papers = _find_paper_by_id(vault_path, folder, paper_id)
        if all_papers:
            papers = [all_papers]
        else:
            papers = [p for p in papers if p["arxiv_id"] == paper_id]
        if not papers:
            print(f"\n❌ 未找到 arXiv ID 为 {paper_id} 的论文")
            return
    else:
        papers = find_interested_papers(vault_path, folder)
        if not papers:
            print("\n📭 没有标记为感兴趣的论文")
            print("   在 Obsidian 中将 status 字段改为 interested 后再运行此命令")
            return

    if not force:
        pending = [p for p in papers if not p["has_deep"]]
    else:
        pending = papers

    if not pending:
        print(f"\n✅ 所有 {len(papers)} 篇收藏论文都已完成深度分析")
        print("   使用 --force 重新分析")
        return

    print(f"\n📋 待深度分析: {len(pending)} 篇论文\n")
    for i, p in enumerate(pending, 1):
        status = "🔄 重新分析" if p["has_deep"] else "🆕 新分析"
        print(f"  {i}. {p.get('icon', '📄')} {p['title'][:60]}")
        print(f"     {p['arxiv_id']} | {status}")

    print()

    provider = ai_cfg.get("provider", "codex")
    model = ai_cfg.get("model", "")

    success = 0
    for i, paper in enumerate(pending, 1):
        print(f"{'─' * 50}")
        print(f"[{i}/{len(pending)}] 🔬 {paper['title'][:60]}...")
        arxiv_id = paper["arxiv_id"]
        abstract = _read_abstract(paper["filepath"])

        # 步骤 1: 搜索代码仓库
        print(f"  📦 搜索代码仓库...")
        repos = search_code_repo(
            title=paper["title"],
            arxiv_id=arxiv_id,
            abstract=abstract,
        )
        print(f"     找到 {len(repos)} 个仓库")
        for r in repos:
            stars_str = f"⭐{r['stars']}" if r['stars'] >= 0 else ""
            print(f"     • {r['url']} ({r['source']}) {stars_str}")

        # 步骤 2: 获取主仓库详情
        repo_detail = None
        if repos:
            main_repo = max(repos, key=lambda x: x.get("stars", 0))
            print(f"  🏠 获取仓库详情: {main_repo['url']}...")
            repo_detail = get_repo_details(main_repo["url"])
            if repo_detail and repo_detail.get("full_name"):
                print(f"     {repo_detail['language'] or 'N/A'} | ⭐{repo_detail['stars']} | 🍴{repo_detail['forks']}")

        # 步骤 3: AI 深度分析
        ai_result = None
        if repo_detail and repo_detail.get("readme"):
            print(f"  🤖 AI 深度分析中 (provider: {provider})...")
            ai_result = ai_deep_analysis(
                title=paper["title"],
                abstract=abstract,
                repo_details=repo_detail,
                provider=provider,
                base_url=ai_cfg.get("base_url", ""),
                api_key=ai_cfg.get("api_key", ""),
                model=model,
                codex_cli_path=ai_cfg.get("codex_cli_path", ""),
                config=config,
            )
            if ai_result:
                print(f"     ✅ AI 分析完成")
            else:
                print(f"     ⚠️ AI 分析失败，仅保留仓库信息")
        elif not repos:
            print(f"  ⚠️ 未找到代码仓库，跳过 AI 深度分析")

        # 步骤 4: 生成深度笔记并追加
        deep_content = generate_deep_note(
            title=paper["title"],
            arxiv_id=arxiv_id,
            abstract=abstract,
            repos=repos,
            repo_details=repo_detail,
            ai_analysis=ai_result,
        )
        append_deep_note(paper["filepath"], deep_content)
        print(f"  📝 深度笔记已写入!")
        success += 1

    print(f"\n{'=' * 50}")
    print(f"✅ 深度分析完成！成功 {success}/{len(pending)} 篇")


def _find_paper_by_id(vault_path: str, folder: str, arxiv_id: str) -> dict | None:
    """根据 arXiv ID 在 vault 中查找论文"""
    papers_dir = os.path.join(vault_path, folder)
    if not os.path.exists(papers_dir):
        return None

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
                fm = yaml.safe_load(content[3:end])
                if fm and fm.get("arxiv_id") == arxiv_id:
                    status = fm.get("status", "")
                    if not status:
                        status = "interested" if fm.get("interested") is True else "unread"
                    return {
                        "filepath": filepath,
                        "title": fm.get("title", ""),
                        "arxiv_id": fm.get("arxiv_id", ""),
                        "code": fm.get("code", ""),
                        "category": fm.get("category", ""),
                        "icon": fm.get("icon", ""),
                        "status": status,
                        "status_updated": fm.get("status_updated", fm.get("interested_at", "")),
                        "has_deep": "## 🔬 二阶段深度分析" in content,
                    }
            except Exception:
                continue
    return None


def _read_abstract(filepath: str) -> str:
    """从论文笔记中提取摘要文本"""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        if content.startswith("---"):
            end = content.find("---", 3)
            if end != -1:
                body = content[end + 3:]
                return body[:1500].strip()
    except Exception:
        pass
    return ""


# =============================================================================
# check 模式：二次质量检查
# =============================================================================

def cmd_check(config: dict, fix: bool = False, recent: int = 0):
    """检查已有论文笔记的质量并可选修复"""
    output_cfg = config.get("output", {})
    vault_path = config.get("obsidian_vault", "")

    if not vault_path:
        print("❌ 错误：请在 config.yaml 中配置 obsidian_vault 路径")
        sys.exit(1)

    folder = output_cfg.get("folder", "papers")

    print("=" * 60)
    print("🔍 论文自动阅读系统 v2 — 二次质量检查")
    if recent > 0:
        print(f"   范围: 最近 {recent} 篇")
    else:
        print("   范围: 全部论文")
    print("=" * 60)

    results = check_vault_papers(vault_path, folder, only_recent=recent)
    print_check_report(results)

    if not results:
        return

    regen = [r for r in results if r.needs_regenerate]
    if regen and fix:
        print(f"\n🔧 自动修复：重新生成 {len(regen)} 篇不合格摘要...\n")
        fixed = 0
        for i, r in enumerate(regen, 1):
            print(f"  [{i}/{len(regen)}] {r.title[:55]}...")
            try:
                ok = regenerate_summary(r.filepath, config)
                if ok:
                    fixed += 1
                    print(f"    ✅ 重新生成成功")
                else:
                    print(f"    ❌ 重新生成失败")
            except Exception as e:
                print(f"    ❌ 异常: {e}")
            if i < len(regen):
                time.sleep(8)
        print(f"\n  🔧 修复完成: {fixed}/{len(regen)} 篇")
    elif regen and not fix:
        print(f"\n💡 有 {len(regen)} 篇可自动修复，运行 `check --fix` 执行修复")


# =============================================================================
# list 模式
# =============================================================================

def cmd_list(config: dict, show_all: bool = False):
    """列出收藏或全部论文"""
    output_cfg = config.get("output", {})
    vault_path = config.get("obsidian_vault", "")

    if not vault_path:
        print("❌ 错误：请在 config.yaml 中配置 obsidian_vault 路径")
        sys.exit(1)

    folder = output_cfg.get("folder", "papers")

    print("=" * 60)
    if show_all:
        print("📋 论文自动阅读系统 v2 — 全部论文")
    else:
        print("⭐ 论文自动阅读系统 v2 — 收藏论文")
    print("=" * 60)

    if show_all:
        papers = _list_all_papers(vault_path, folder)
    else:
        papers = find_interested_papers(vault_path, folder)

    if not papers:
        if show_all:
            print("\n📭 还没有任何论文笔记")
        else:
            print("\n📭 还没有收藏任何论文")
            print("   在 Obsidian 中将 status 字段改为 interested")
        return

    # 按 status 分组显示
    STATUS_ORDER = ["reading", "interested", "unread", "done", "rejected"]
    STATUS_ICON = {"unread": "📬", "interested": "⭐", "rejected": "🗑️", "reading": "📖", "done": "✅"}

    by_status: dict[str, list[dict]] = {}
    for p in papers:
        s = p.get("status", "unread")
        by_status.setdefault(s, []).append(p)

    total_shown = 0
    for status_key in STATUS_ORDER:
        group = by_status.pop(status_key, [])
        if not group:
            continue
        s_icon = STATUS_ICON.get(status_key, "📄")
        print(f"\n{s_icon} {status_key} ({len(group)} 篇):")
        for p in group:
            icon = p.get("icon", "📄")
            category = p.get("category", "")
            deep_tag = " 🔬" if p.get("has_deep") else ""
            cat_str = f"[{category}] " if category else ""
            total_shown += 1
            print(f"  {total_shown}. {icon} {cat_str}{p['title'][:55]}{deep_tag}")
            print(f"     {p['arxiv_id']}")
    # 处理未知状态
    for status_key, group in by_status.items():
        if group:
            print(f"\n❓ {status_key} ({len(group)} 篇):")
            for p in group:
                total_shown += 1
                print(f"  {total_shown}. {p.get('icon', '📄')} {p['title'][:55]}")
                print(f"     {p['arxiv_id']}")

    deep_count = sum(1 for p in papers if p.get("has_deep"))
    status_counts = {s: len(g) for s, g in {k: [p for p in papers if p.get('status', 'unread') == k] for k in STATUS_ORDER}.items() if g}
    counts_str = " | ".join(f"{STATUS_ICON.get(k, '?')} {k} {v}" for k, v in status_counts.items())
    print(f"\n{'─' * 40}")
    print(f"  📊 总计 {len(papers)} | {counts_str} | 🔬 深度 {deep_count}")


def _list_all_papers(vault_path: str, folder: str) -> list[dict]:
    """列出 vault 中的所有论文"""
    papers = []
    papers_dir = os.path.join(vault_path, folder)
    if not os.path.exists(papers_dir):
        return papers

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
                fm = yaml.safe_load(content[3:end])
                if not fm or not fm.get("arxiv_id"):
                    continue
                status = fm.get("status", "")
                # 兼容旧版 interested 字段
                if not status:
                    if fm.get("interested") is True:
                        status = "interested"
                    else:
                        status = "unread"
                papers.append({
                    "filepath": filepath,
                    "filename": f[:-3],  # 去掉 .md，用于 wikilink
                    "title": fm.get("title", ""),
                    "arxiv_id": fm.get("arxiv_id", ""),
                    "category": fm.get("category", ""),
                    "icon": fm.get("icon", ""),
                    "status": status,
                    "status_updated": fm.get("status_updated", fm.get("interested_at", "")),
                    "created": fm.get("created", ""),
                    "has_deep": "## 🔬 二阶段深度分析" in content,
                })
            except Exception:
                continue

    papers.sort(key=lambda x: x.get("status_updated") or x.get("created") or "", reverse=True)
    return papers


# =============================================================================
# cleanup 模式：清理不感兴趣的论文
# =============================================================================

def cmd_cleanup(config: dict, days: int = 3, confirm: bool = False):
    """清理不感兴趣的论文（删除文件 + 记录到 history.json）"""
    output_cfg = config.get("output", {})
    vault_path = config.get("obsidian_vault", "")

    if not vault_path:
        print("错误：请在 config.yaml 中配置 obsidian_vault 路径")
        sys.exit(1)

    folder = output_cfg.get("folder", "papers")

    print("=" * 60)
    print("   论文自动阅读系统 v2 — 清理不感兴趣论文")
    print(f"   Vault: {vault_path}")
    print(f"   最低天数: {days} 天前创建的论文才会被清理")
    print("=" * 60)

    all_papers = _list_all_papers(vault_path, folder)
    if not all_papers:
        print("\n没有找到任何论文")
        return

    # 筛选：status == "rejected" 且创建时间超过 days 天
    now = datetime.now(timezone.utc)
    to_clean = []
    for p in all_papers:
        if p.get("status") != "rejected":
            continue  # 只清理明确标记为 rejected 的论文

        # 检查创建时间
        created_str = p.get("created", "")
        if created_str:
            try:
                created = datetime.strptime(str(created_str)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                age_days = (now - created).days
                if age_days < days:
                    continue  # 太新，防止误操作
            except (ValueError, TypeError):
                pass

        to_clean.append(p)

    if not to_clean:
        print(f"\n没有需要清理的论文（没有 status: rejected 的论文，或创建不足 {days} 天）")
        return

    # 按分类分组显示
    by_category: dict[str, list[dict]] = {}
    for p in to_clean:
        cat = p.get("category", "通用")
        by_category.setdefault(cat, []).append(p)

    print(f"\n找到 {len(to_clean)} 篇已拒绝的论文（共 {len(all_papers)} 篇中）:\n")
    for cat, papers in sorted(by_category.items()):
        print(f"  [{cat}] ({len(papers)} 篇)")
        for p in papers:
            print(f"    - {p['title'][:60]}")

    if not confirm:
        print(f"\n--- 预览模式 ---")
        print(f"  将清理 {len(to_clean)} 篇论文（及对应 PDF）")
        print(f"  其 arXiv ID 将记录到 history.json，后续扫描不会重复收集")
        print(f"\n  确认删除请运行: cleanup --confirm" + (f" --days {days}" if days != 3 else ""))
        return

    # 执行清理
    deleted = 0
    arxiv_ids = []
    for p in to_clean:
        filepath = p["filepath"]
        arxiv_id = p["arxiv_id"]
        try:
            # 删除关联的 PDF
            paper_dir = os.path.dirname(filepath)
            pdf_dir = os.path.join(paper_dir, "pdf")
            safe_title = os.path.splitext(os.path.basename(filepath))[0]
            pdf_path = os.path.join(pdf_dir, f"{safe_title}.pdf")
            if os.path.exists(pdf_path):
                os.remove(pdf_path)

            # 删除笔记文件
            os.remove(filepath)
            arxiv_ids.append(arxiv_id)
            deleted += 1
            print(f"  [删除] {p['title'][:55]}  ({arxiv_id})")
        except Exception as e:
            print(f"  [失败] {p['title'][:55]}: {e}")

    # 记录到 history.json
    if arxiv_ids:
        from .filters import mark_ids_processed
        mark_ids_processed(arxiv_ids)

    print(f"\n完成！删除 {deleted} 篇论文，已记录 {len(arxiv_ids)} 个 ID 到 history.json")
    log_activity("cleanup", {"deleted": deleted, "ids_recorded": len(arxiv_ids)})


# =============================================================================
# sync-history 模式：同步 vault 到 history.json
# =============================================================================

def cmd_sync_history(config: dict):
    """将 vault 中所有论文的 ID 同步到 history.json"""
    output_cfg = config.get("output", {})
    vault_path = config.get("obsidian_vault", "")

    if not vault_path:
        print("错误：请在 config.yaml 中配置 obsidian_vault 路径")
        sys.exit(1)

    folder = output_cfg.get("folder", "papers")

    print("=" * 60)
    print("   论文自动阅读系统 v2 — 同步历史记录")
    print(f"   Vault: {vault_path}/{folder}")
    print("=" * 60)

    new_count = sync_vault_to_history(vault_path, folder)
    from .filters import load_history
    total = len(load_history())

    print(f"\n同步完成! 新增 {new_count} 个 ID，history.json 总计 {total} 条记录")
    print(f"后续扫描将自动跳过这些已收集的论文")


# =============================================================================
# migrate 模式：迁移旧版 interested 字段到 status
# =============================================================================

def cmd_migrate(config: dict):
    """将 vault 中旧版 interested/interested_at 字段迁移为 status/status_updated"""
    output_cfg = config.get("output", {})
    vault_path = config.get("obsidian_vault", "")

    if not vault_path:
        print("错误：请在 config.yaml 中配置 obsidian_vault 路径")
        sys.exit(1)

    folder = output_cfg.get("folder", "papers")
    papers_dir = os.path.join(vault_path, folder)

    print("=" * 60)
    print("   论文自动阅读系统 v2 — 迁移旧版字段")
    print(f"   Vault: {vault_path}/{folder}")
    print("=" * 60)

    if not os.path.exists(papers_dir):
        print("\n没有找到 papers 目录")
        return

    migrated = 0
    skipped = 0
    errors = 0

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

                # 已经有 status 字段，跳过
                if "status" in fm:
                    skipped += 1
                    continue

                # 没有 interested 字段也没有 status，需要迁移
                old_interested = fm.get("interested", False)
                old_interested_at = fm.get("interested_at", "")

                new_status = "interested" if old_interested is True else "unread"
                new_status_updated = old_interested_at or ""

                # 替换 frontmatter 中的字段
                new_fm_text = fm_text
                if "interested_at:" in new_fm_text:
                    new_fm_text = re.sub(
                        r'^interested_at:.*$',
                        f'status_updated: "{new_status_updated}"',
                        new_fm_text, flags=re.MULTILINE,
                    )
                else:
                    new_fm_text += f'\nstatus_updated: "{new_status_updated}"'

                if "interested:" in new_fm_text:
                    new_fm_text = re.sub(
                        r'^interested:.*$',
                        f'status: {new_status}',
                        new_fm_text, flags=re.MULTILINE,
                    )
                else:
                    new_fm_text += f'\nstatus: {new_status}'

                new_content = f"---{new_fm_text}---{content[end + 3:]}"

                # 同时更新 tip box（如果存在旧版）
                old_tip_v1 = (
                    "> [!tip] 快速操作\n"
                    "> - [ ] ⭐ **标记为感兴趣** — 在 Properties 面板直接勾选 `interested`，时间戳自动填入\n"
                    "> - 快捷键：`Ctrl+Shift+I` 一键切换"
                )
                old_tip_v2 = (
                    "> [!info] 论文状态\n"
                    "> 在 Properties 面板中修改 `status` 字段：\n"
                    "> - **unread** — 尚未审阅（默认）\n"
                    "> - **interested** — 感兴趣，需要细读\n"
                    "> - **rejected** — 不感兴趣，将被自动清理\n"
                    "> - **reading** — 正在阅读中\n"
                    "> - **done** — 已读完"
                )
                new_tip = (
                    "> [!info] 论文状态\n"
                    "> 点击下方链接快速切换 status（需安装 Meta Bind 插件），或在 Properties 面板中手动修改：\n"
                    "> `INPUT[inlineSelect(option(unread, 📬 未读), option(interested, ⭐ 感兴趣), option(rejected, 🗑️ 不感兴趣), option(reading, 📖 阅读中), option(done, ✅ 已读)):status]`"
                )
                new_content = new_content.replace(old_tip_v1, new_tip)
                new_content = new_content.replace(old_tip_v2, new_tip)

                with open(filepath, "w", encoding="utf-8") as fh:
                    fh.write(new_content)
                migrated += 1
                print(f"  [迁移] {fm.get('title', f)[:55]}  interested={old_interested} -> status={new_status}")

            except Exception as e:
                errors += 1
                print(f"  [错误] {f}: {e}")

    print(f"\n完成！迁移 {migrated} 篇，跳过 {skipped} 篇（已有 status），错误 {errors} 篇")


# =============================================================================
# stats 模式：阅读统计面板
# =============================================================================

def cmd_stats(config: dict):
    """显示阅读统计概览"""
    from collections import Counter

    output_cfg = config.get("output", {})
    vault_path = config.get("obsidian_vault", "")
    if not vault_path:
        print("错误：请在 config.yaml 中配置 obsidian_vault 路径")
        sys.exit(1)

    folder = output_cfg.get("folder", "papers")
    papers = _list_all_papers(vault_path, folder)

    if not papers:
        print("还没有任何论文笔记")
        return

    print("=" * 60)
    print("   论文自动阅读系统 v2 — 阅读统计")
    print("=" * 60)

    # 1. 状态分布
    status_counter = Counter(p.get("status", "unread") for p in papers)
    STATUS_ICON = {"unread": "📬", "interested": "⭐", "rejected": "🗑️", "reading": "📖", "done": "✅"}
    print(f"\n📊 状态分布 (共 {len(papers)} 篇):")
    for s in ["unread", "interested", "reading", "done", "rejected"]:
        cnt = status_counter.get(s, 0)
        if cnt:
            bar = "█" * min(cnt, 40)
            icon = STATUS_ICON.get(s, "?")
            print(f"  {icon} {s:<12} {cnt:>4}  {bar}")

    # 2. 分类分布
    cat_counter = Counter(p.get("category", "未分类") for p in papers)
    print(f"\n📂 分类分布:")
    for cat, cnt in cat_counter.most_common():
        bar = "█" * min(cnt, 30)
        print(f"  {cat:<14} {cnt:>4}  {bar}")

    # 3. 月度趋势（按创建时间）
    month_counter = Counter()
    for p in papers:
        created = p.get("created", "")
        if created:
            month = str(created)[:7]  # YYYY-MM
            if len(month) == 7:
                month_counter[month] += 1
    if month_counter:
        print(f"\n📅 月度论文数:")
        for month in sorted(month_counter.keys()):
            cnt = month_counter[month]
            bar = "█" * min(cnt, 30)
            print(f"  {month}  {cnt:>4}  {bar}")

    # 4. 阅读率
    total = len(papers)
    read_count = status_counter.get("interested", 0) + status_counter.get("reading", 0) + status_counter.get("done", 0)
    reject_count = status_counter.get("rejected", 0)
    unread_count = status_counter.get("unread", 0)
    deep_count = sum(1 for p in papers if p.get("has_deep"))

    print(f"\n📈 核心指标:")
    print(f"  阅读率:     {read_count}/{total} ({100*read_count//total if total else 0}%)")
    print(f"  淘汰率:     {reject_count}/{total} ({100*reject_count//total if total else 0}%)")
    print(f"  待审阅:     {unread_count}")
    print(f"  深度分析:   {deep_count}")

    # 5. 兴趣分类偏好（interested + reading + done 的分类分布）
    interest_cats = Counter(
        p.get("category", "未分类") for p in papers
        if p.get("status") in ("interested", "reading", "done")
    )
    if interest_cats:
        print(f"\n🎯 兴趣偏好 (按收藏分类):")
        for cat, cnt in interest_cats.most_common(5):
            print(f"  {cat}: {cnt} 篇")


# =============================================================================
# dashboard 模式：生成 Obsidian 统计面板笔记
# =============================================================================

def cmd_dashboard(config: dict):
    """生成 Obsidian 统计面板 _dashboard.md"""
    from collections import Counter

    output_cfg = config.get("output", {})
    vault_path = config.get("obsidian_vault", "")
    if not vault_path:
        print("错误：请在 config.yaml 中配置 obsidian_vault 路径")
        sys.exit(1)

    folder = output_cfg.get("folder", "papers")
    papers = _list_all_papers(vault_path, folder)

    if not papers:
        print("还没有任何论文笔记")
        return

    # 统计数据
    status_counter = Counter(p.get("status", "unread") for p in papers)
    cat_counter = Counter(p.get("category", "未分类") for p in papers)
    total = len(papers)
    read_count = status_counter.get("interested", 0) + status_counter.get("reading", 0) + status_counter.get("done", 0)
    reject_count = status_counter.get("rejected", 0)
    unread_count = status_counter.get("unread", 0)
    deep_count = sum(1 for p in papers if p.get("has_deep"))

    # 月度趋势
    month_counter = Counter()
    for p in papers:
        created = p.get("created", "")
        if created:
            month = str(created)[:7]
            if len(month) == 7:
                month_counter[month] += 1

    # 兴趣分类
    interest_cats = Counter(
        p.get("category", "未分类") for p in papers
        if p.get("status") in ("interested", "reading", "done")
    )

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 构建 Markdown
    read_pct = 100*read_count//total if total else 0
    reject_pct = 100*reject_count//total if total else 0

    lines = [
        "---",
        "title: 论文阅读仪表板",
        f"updated: {now_str}",
        "tags: [统计, 自动生成]",
        "cssclass: dashboard",
        "---",
        "",
        "# 📊 论文阅读仪表板",
        "",
        f"> [!info] 自动更新于 {now_str}",
        f"> 📬 总计 **{total}** 篇 · ⭐ 收藏 **{read_count}** ({read_pct}%) · "
        f"🗑️ 淘汰 **{reject_count}** ({reject_pct}%) · 📬 待审 **{unread_count}** · "
        f"🔬 深度 **{deep_count}**",
        "",
        "---",
        "",
        "## � 状态总览",
        "",
        "```mermaid",
        "pie title 论文状态分布",
    ]

    STATUS_LABELS = {"unread": "待审阅", "interested": "感兴趣", "reading": "阅读中", "done": "已完成", "rejected": "已淘汰"}
    for s in ["unread", "interested", "reading", "done", "rejected"]:
        cnt = status_counter.get(s, 0)
        if cnt:
            lines.append(f'    "{STATUS_LABELS.get(s, s)}" : {cnt}')
    lines.extend(["```", ""])

    # 状态快速导航
    nav_parts = []
    for s in ["unread", "interested", "reading", "done", "rejected"]:
        cnt = status_counter.get(s, 0)
        label = STATUS_LABELS.get(s, s)
        nav_parts.append(f"[[_{s}|{label} ({cnt})]]")
    lines.append("**快速导航:** " + " · ".join(nav_parts))
    lines.extend(["", "---", "", "## 📂 分类分布", ""])

    # 分类表格
    lines.extend([
        "| 分类 | 数量 | 占比 |",
        "|------|------|------|",
    ])
    for cat, cnt in cat_counter.most_common():
        pct = f"{100*cnt//total}%" if total else "0%"
        lines.append(f"| {cat} | {cnt} | {pct} |")
    lines.append("")

    # 月度趋势
    if month_counter:
        lines.extend(["---", "", "## 📅 月度趋势", "", "```mermaid", "xychart-beta",
                       '    title "月度新增论文数"',
                       '    x-axis [' + ", ".join(f'"{m}"' for m in sorted(month_counter.keys())) + "]",
                       '    y-axis "数量"',
                       "    bar [" + ", ".join(str(month_counter[m]) for m in sorted(month_counter.keys())) + "]",
                       "```", ""])

    # 兴趣偏好
    if interest_cats:
        lines.extend(["---", "", "## 🎯 兴趣偏好", ""])
        lines.extend([
            "```mermaid",
            "pie title 收藏论文分类",
        ])
        for cat, cnt in interest_cats.most_common():
            lines.append(f'    "{cat}" : {cnt}')
        lines.extend(["```", ""])

    # 最近收藏
    recent_interested = [p for p in papers if p.get("status") in ("interested", "reading")]
    if recent_interested:
        lines.extend(["---", "", "## ⭐ 最近收藏", ""])
        for p in recent_interested[:10]:
            icon = p.get("icon", "📄")
            cat = p.get("category", "")
            cat_str = f"[{cat}] " if cat else ""
            fname = p.get("filename", p["title"])
            lines.append(f"- {icon} {cat_str}[[{fname}]]")
        lines.append("")

    # AI 推荐优先阅读（基于反馈画像打分）
    from .interest_tracker import build_feedback_profile, score_paper_relevance
    unread_papers = [p for p in papers if p.get("status") == "unread"]
    if unread_papers:
        feedback_profile = build_feedback_profile(vault_path, folder)
        if feedback_profile:
            for p in unread_papers:
                p["_relevance"] = score_paper_relevance(
                    p.get("title", ""), p.get("category", ""), feedback_profile
                )
            scored = sorted(unread_papers, key=lambda x: x.get("_relevance", 0), reverse=True)
            top_papers = [p for p in scored[:10] if p.get("_relevance", 0) > 0]
            if top_papers:
                lines.extend(["---", "", "## 🤖 AI 推荐优先阅读", ""])
                lines.append("> [!success] 基于你的收藏/淘汰偏好自动排序，相关度越高越推荐")
                lines.append("")
                lines.extend(["| # | 论文 | 分类 | 相关度 |",
                               "|---|------|------|--------|"])
                for i, p in enumerate(top_papers, 1):
                    cat = p.get("category", "")
                    score = p.get("_relevance", 0)
                    fname = p.get("filename", p["title"])
                    lines.append(f"| {i} | [[{fname}]] | {cat} | {'★' * min(int(score * 5), 5)} {score:.2f} |")
                lines.append("")

    # 待审阅
    recent_unread = [p for p in papers if p.get("status") == "unread"]
    if recent_unread:
        lines.extend(["---", "", "## 📬 待审阅", ""])
        for p in recent_unread[:10]:
            icon = p.get("icon", "📄")
            cat = p.get("category", "")
            cat_str = f"[{cat}] " if cat else ""
            fname = p.get("filename", p["title"])
            lines.append(f"- {icon} {cat_str}[[{fname}]]")
        if len(recent_unread) > 10:
            lines.append(f"- ... 还有 {len(recent_unread) - 10} 篇")
        lines.append("")

    # 正反关键词词云（每次 dashboard 时重新构建，保证数据新鲜）
    from .interest_tracker import build_keyword_profile as _build_kw
    kw_profile = _build_kw(vault_path, folder)
    if kw_profile:
        lines.extend(["---", "", "## 🔑 研究兴趣画像", ""])
        lines.append(f"> [!abstract] 基于 {kw_profile.get('positive_count', 0)} 篇感兴趣 + "
                      f"{kw_profile.get('negative_count', 0)} 篇已淘汰论文构建 | "
                      f"更新于 {kw_profile.get('updated', 'N/A')}")
        lines.append("")

        pos_phrases = kw_profile.get("positive_phrases", [])
        neg_phrases = kw_profile.get("negative_phrases", [])

        if pos_phrases:
            lines.append("**✅ 感兴趣方向**")
            lines.append("")
            # 用 HTML span + font-size 实现词云效果
            cloud_parts = []
            max_net = max(p.get("net", p["count"]) for p in pos_phrases) if pos_phrases else 1
            for item in pos_phrases[:15]:
                phrase = item["phrase"]
                net = item.get("net", item["count"])
                # 映射 net 分数到字体大小 (1.0em ~ 2.2em)
                ratio = net / max_net if max_net > 0 else 0.5
                size = 1.0 + ratio * 1.2
                color = "#2ecc71" if ratio >= 0.6 else "#27ae60" if ratio >= 0.3 else "#95a5a6"
                cloud_parts.append(
                    f'<span style="font-size:{size:.1f}em;color:{color};font-weight:bold">{phrase}</span>'
                )
            lines.append('<p style="line-height:2.2em;text-align:center">')
            lines.append(" &nbsp;·&nbsp; ".join(cloud_parts))
            lines.append("</p>")
            lines.append("")

        if neg_phrases:
            lines.append("**❌ 不感兴趣方向**")
            lines.append("")
            cloud_parts = []
            for item in neg_phrases[:8]:
                phrase = item["phrase"]
                cloud_parts.append(
                    f'<span style="font-size:0.9em;color:#e74c3c;text-decoration:line-through">{phrase}</span>'
                )
            lines.append('<p style="line-height:1.8em;text-align:center">')
            lines.append(" &nbsp;·&nbsp; ".join(cloud_parts))
            lines.append("</p>")
            lines.append("")
    else:
        lines.extend(["---", "", "## 🔑 研究兴趣画像", ""])
        lines.append("> [!warning] 尚未生成关键词画像")
        lines.append(f"> 运行 `python -m paper_reader_v2.main update-keywords` 生成")
        lines.append("")

    # 活动日志
    from .filters import get_activity_log
    activity = get_activity_log(days=14)
    if activity:
        lines.extend(["---", "", "## 📋 最近活动", ""])
        ACTION_ICONS = {
            "scan": "🔍", "fix": "🔧", "cleanup": "🧹",
            "update_keywords": "🔑", "dashboard": "📊",
        }
        MAX_VISIBLE = 20
        entry_count = 0
        overflow_lines = []
        for date in sorted(activity.keys(), reverse=True):
            date_header = f"**{date}**"
            date_entries = []
            for entry in reversed(activity[date]):
                action = entry.get("action", "")
                time_str = entry.get("time", "")[:5]  # HH:MM
                icon = ACTION_ICONS.get(action, "▪️")
                # 简洁详情
                detail_parts = []
                for k, v in entry.items():
                    if k in ("time", "action"):
                        continue
                    if isinstance(v, list):
                        detail_parts.append(f"{', '.join(str(i) for i in v[:3])}")
                    else:
                        detail_parts.append(f"{k}={v}")
                detail = " · ".join(detail_parts) if detail_parts else ""
                date_entries.append(f"- {icon} `{time_str}` **{action}** {detail}")
            if entry_count >= MAX_VISIBLE:
                overflow_lines.append(date_header)
                overflow_lines.append("")
                overflow_lines.extend(date_entries)
                overflow_lines.append("")
            elif entry_count + len(date_entries) <= MAX_VISIBLE:
                lines.append(date_header)
                lines.append("")
                lines.extend(date_entries)
                lines.append("")
            else:
                visible_count = MAX_VISIBLE - entry_count
                lines.append(date_header)
                lines.append("")
                lines.extend(date_entries[:visible_count])
                lines.append("")
                overflow_lines.append(date_header)
                overflow_lines.append("")
                overflow_lines.extend(date_entries[visible_count:])
                overflow_lines.append("")
            entry_count += len(date_entries)
        if overflow_lines:
            lines.append("")
            lines.append(f"> [!note]- 📜 显示更多活动（还有 {entry_count - MAX_VISIBLE} 条）")
            for ol in overflow_lines:
                lines.append(f"> {ol}" if ol else ">")
            lines.append("")

    # 操作入口 — 交互按钮（dataviewjs）
    project_dir = os.path.dirname(os.path.abspath(__file__))
    cwd_dir = os.path.dirname(project_dir).replace("\\", "/")
    python_exe = sys.executable.replace("\\", "/")

    lines.extend([
        "---", "",
        "## ⚙️ 操作面板", "",
        "```dataviewjs",
        "const cwd = '" + cwd_dir + "';",
        "const py = '" + python_exe + "';",
        "const cp = require('child_process');",
        "const root = dv.container;",
        "",
        "// --- 按钮行 ---",
        "const btnRow = root.createEl('div');",
        "btnRow.style.display = 'flex';",
        "btnRow.style.gap = '8px';",
        "btnRow.style.flexWrap = 'wrap';",
        "",
        "// --- 实时日志区 ---",
        "const logWrap = root.createEl('div');",
        "logWrap.style.marginTop = '10px';",
        "logWrap.style.display = 'none';",
        "const logHeader = logWrap.createEl('div');",
        "logHeader.style.display = 'flex';",
        "logHeader.style.justifyContent = 'space-between';",
        "logHeader.style.alignItems = 'center';",
        "logHeader.style.marginBottom = '4px';",
        "const logTitle = logHeader.createEl('span', {text: '📋 运行日志'});",
        "logTitle.style.fontWeight = 'bold';",
        "const clearBtn = logHeader.createEl('button', {text: '✕ 关闭'});",
        "clearBtn.style.fontSize = '12px';",
        "clearBtn.style.cursor = 'pointer';",
        "clearBtn.onclick = () => { logWrap.style.display = 'none'; };",
        "const logBox = logWrap.createEl('pre');",
        "logBox.style.cssText = 'max-height:300px;overflow-y:auto;padding:8px;border:1px solid var(--background-modifier-border);border-radius:6px;font-size:12px;white-space:pre-wrap;word-break:break-all;background:var(--background-secondary);';",
        "",
        "function strip(s) { return s.replace(/\x1b\[[0-9;]*m/g, ''); }",
        "function appendLog(text) {",
        "  logBox.textContent += strip(text);",
        "  logBox.scrollTop = logBox.scrollHeight;",
        "}",
        "",
        "// --- 快速命令（exec）---",
        "const quickCmds = [",
        "  ['🔄 刷新仪表板', 'dashboard', true],",
        "  ['🔑 更新关键词', 'update-keywords', false],",
        "];",
        "for (const [label, mod, primary] of quickCmds) {",
        "  const btn = btnRow.createEl('button', {text: label});",
        "  if (primary) btn.className = 'mod-cta';",
        "  btn.style.padding = '6px 16px';",
        "  btn.style.cursor = 'pointer';",
        "  btn.onclick = () => {",
        "    new Notice(label + ' 执行中...');",
        "    btn.disabled = true;",
        "    btn.textContent = label + ' ⏳';",
        "    cp.exec(py + ' -m paper_reader_v2.main ' + mod,",
        "      {cwd: cwd, timeout: 600000, env: {...process.env, PYTHONIOENCODING: 'utf-8'}},",
        "      (err, stdout, stderr) => {",
        "        btn.disabled = false;",
        "        btn.textContent = label;",
        "        if (!err) {",
        "          const ls = (stdout || '').trim().split('\\n');",
        "          const last = ls.filter(l => l.trim()).slice(-3).join('\\n');",
        "          new Notice(label + ' 完成！\\n' + last, 10000);",
        "        } else {",
        "          const msg = (stderr || err.message || '').trim().split('\\n').slice(-2).join('\\n');",
        "          new Notice('❌ ' + label + ' 失败:\\n' + msg, 10000);",
        "        }",
        "      }",
        "    );",
        "  };",
        "}",
        "",
        "// --- 扫描按钮（spawn 实时输出）---",
        "const scanBtn = btnRow.createEl('button', {text: '🔍 扫描新论文'});",
        "scanBtn.style.padding = '6px 16px';",
        "scanBtn.style.cursor = 'pointer';",
        "scanBtn.onclick = () => {",
        "  scanBtn.disabled = true;",
        "  scanBtn.textContent = '🔍 扫描中 ⏳';",
        "  logBox.textContent = '';",
        "  logWrap.style.display = 'block';",
        "  appendLog('⏳ 开始扫描...\\n\\n');",
        "  const proc = cp.spawn(py, ['-m', 'paper_reader_v2.main', 'scan'], {",
        "    cwd: cwd,",
        "    env: {...process.env, PYTHONIOENCODING: 'utf-8'},",
        "  });",
        "  proc.stdout.on('data', (d) => appendLog(d.toString()));",
        "  proc.stderr.on('data', (d) => { const s = d.toString(); if (!/LiteLLM:|litellm/.test(s)) appendLog('[stderr] ' + s); });",
        "  proc.on('close', (code) => {",
        "    scanBtn.disabled = false;",
        "    scanBtn.textContent = '🔍 扫描新论文';",
        "    if (code === 0) {",
        "      appendLog('\\n✅ 扫描完成');",
        "      new Notice('🔍 扫描完成！查看上方日志了解详情', 8000);",
        "    } else {",
        "      appendLog('\\n❌ 扫描异常退出 (code ' + code + ')');",
        "      new Notice('❌ 扫描失败，查看日志了解原因', 8000);",
        "    }",
        "  });",
        "  proc.on('error', (e) => {",
        "    scanBtn.disabled = false;",
        "    scanBtn.textContent = '🔍 扫描新论文';",
        "    appendLog('\\n❌ 启动失败: ' + e.message);",
        "    new Notice('❌ 启动失败: ' + e.message, 8000);",
        "  });",
        "};",
        "```",
        "",
    ])

    lines.extend([
        "> [!tip] 终端命令",
        f"> `{python_exe} -m paper_reader_v2.main stats` — 查看完整统计",
        "",
    ])

    # 写入文件
    dashboard_path = os.path.join(vault_path, folder, "_dashboard.md")
    content = "\n".join(lines)
    with open(dashboard_path, "w", encoding="utf-8") as f:
        f.write(content)

    # 生成状态索引页
    status_groups = {}
    for p in papers:
        s = p.get("status", "unread")
        status_groups.setdefault(s, []).append(p)

    STATUS_META = {
        "unread":     {"label": "待审阅", "icon": "📬", "desc": "尚未审阅的论文"},
        "interested": {"label": "感兴趣", "icon": "⭐", "desc": "标记为感兴趣的论文"},
        "reading":    {"label": "阅读中", "icon": "📖", "desc": "正在阅读的论文"},
        "done":       {"label": "已完成", "icon": "✅", "desc": "已完成阅读的论文"},
        "rejected":   {"label": "已淘汰", "icon": "🗑️", "desc": "标记为不感兴趣的论文"},
    }

    generated_status_pages = []
    for status_key, meta in STATUS_META.items():
        # 使用 Dataview 动态查询，实时反映论文状态变化
        page_lines = [
            "---",
            f"title: {meta['label']}论文索引",
            "tags: [索引, 动态]",
            "---",
            "",
            f"# {meta['icon']} {meta['label']}论文",
            "",
            f"> {meta['desc']} · 内容由 Dataview 实时查询，无需手动刷新",
            "",
            f"← [[_dashboard|返回仪表板]]",
            "",
            "```dataviewjs",
            f"const status = '{status_key}';",
            "const pages = dv.pages('\"" + folder + "\"')",
            f"  .where(p => p.status === status && p.arxiv_id);",
            "",
            "if (pages.length === 0) {",
            "  dv.paragraph('*暂无论文*');",
            "} else {",
            "  dv.paragraph(`共 ${pages.length} 篇`);",
            "  const groups = pages.groupBy(p => p.category || '未分类');",
            "  for (const group of groups.sort(g => g.key, 'asc')) {",
            "    dv.header(2, `${group.key} (${group.rows.length} 篇)`);",
            "    dv.list(group.rows.map(p => {",
            "      const icon = p.icon || '📄';",
            "      return `${icon} [[${p.file.name}]]`;",
            "    }));",
            "  }",
            "}",
            "```",
            "",
        ]

        page_path = os.path.join(vault_path, folder, f"_{status_key}.md")
        with open(page_path, "w", encoding="utf-8") as f:
            f.write("\n".join(page_lines))
        generated_status_pages.append(status_key)

    print(f"仪表板已生成: {dashboard_path}")
    print(f"  总计 {total} 篇 | 收藏 {read_count} | 待审 {unread_count} | 深度 {deep_count}")
    print(f"  状态索引页: {', '.join(f'_{s}.md' for s in generated_status_pages)}")
    log_activity("dashboard", {"total": total, "read": read_count, "unread": unread_count})


# =============================================================================
# update-keywords 模式：根据反馈更新正反关键词画像
# =============================================================================

def cmd_update_keywords(config: dict):
    """根据 interested/rejected 论文构建正反关键词画像"""
    from .interest_tracker import build_keyword_profile

    output_cfg = config.get("output", {})
    vault_path = config.get("obsidian_vault", "")
    if not vault_path:
        print("错误：请在 config.yaml 中配置 obsidian_vault 路径")
        sys.exit(1)

    folder = output_cfg.get("folder", "papers")

    print("=" * 60)
    print("   论文自动阅读系统 v2 — 更新关键词画像")
    print("=" * 60)

    profile = build_keyword_profile(vault_path, folder)

    print(f"\n基于 {profile['positive_count']} 篇感兴趣 + {profile['negative_count']} 篇已淘汰论文")

    if profile["positive_phrases"]:
        print(f"\n✅ 正面关键词 (感兴趣的方向 — 仅净偏正的词):")
        for item in profile["positive_phrases"][:15]:
            net = item.get("net", 0)
            bar = "█" * min(item["count"], 20)
            print(f"  {item['phrase']:<35} x{item['count']:>2}  net={net:.3f}  {bar}")

    if profile["negative_phrases"]:
        print(f"\n❌ 负面关键词 (不感兴趣的方向 — 仅净偏负的词):")
        for item in profile["negative_phrases"][:10]:
            net = item.get("net", 0)
            bar = "░" * min(item["count"], 20)
            print(f"  {item['phrase']:<35} x{item['count']:>2}  net={net:.3f}  {bar}")

    if profile["positive_categories"]:
        print(f"\n📂 分类偏好:")
        for cat, cnt in profile["positive_categories"].items():
            print(f"  ⭐ {cat}: {cnt} 篇")
    if profile["negative_categories"]:
        for cat, cnt in profile["negative_categories"].items():
            print(f"  🗑️ {cat}: {cnt} 篇")

    print(f"\n画像已保存到 keyword_profile.json")

    # ---- 自动将正面关键词回写到 config.yaml 搜索列表（保留注释） ----
    synced_keywords: list[str] = []
    config_path = Path(__file__).parent / "config.yaml"
    AUTO_MARKER = "# --- 自动发现（来自兴趣画像） ---"
    try:
        # 读取原始文件行（保留注释）
        with open(config_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        # 解析现有手动关键词（排除旧的自动发现区域）
        manual_kw_lower: set[str] = set()
        in_keywords = False
        in_auto = False
        for line in lines:
            stripped = line.strip()
            if stripped == "keywords:" or stripped.startswith("keywords:"):
                in_keywords = True
                continue
            if in_keywords:
                if stripped == AUTO_MARKER:
                    in_auto = True
                    continue
                if stripped.startswith("- "):
                    if not in_auto:
                        kw = stripped[2:].strip().strip('"').strip("'")
                        manual_kw_lower.add(kw.lower())
                elif stripped and not stripped.startswith("#"):
                    break

        # 取净偏正分数最高的短语，过滤已被手动关键词覆盖的
        for item in profile.get("positive_phrases", []):
            phrase = item["phrase"]
            if any(phrase in ex or ex in phrase for ex in manual_kw_lower):
                continue
            search_term = f"{phrase} LLM"
            if search_term.lower() not in manual_kw_lower:
                synced_keywords.append(search_term)
            if len(synced_keywords) >= 5:
                break

        # 先删除旧的自动发现区域
        new_lines: list[str] = []
        in_keywords = False
        in_auto = False
        for line in lines:
            stripped = line.strip()
            if stripped == "keywords:" or stripped.startswith("keywords:"):
                in_keywords = True
            if in_keywords and stripped == AUTO_MARKER:
                in_auto = True
                continue
            if in_auto:
                if stripped.startswith("- "):
                    continue  # 跳过旧的自动关键词
                else:
                    in_auto = False  # 自动区域结束
            new_lines.append(line)
        lines = new_lines

        if synced_keywords:
            # 找到 keywords 区域的末尾（最后一个 "- " 行）
            last_kw_idx = -1
            in_keywords = False
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped == "keywords:" or stripped.startswith("keywords:"):
                    in_keywords = True
                    continue
                if in_keywords:
                    if stripped.startswith("- ") or stripped.startswith("#"):
                        last_kw_idx = i
                    elif stripped:
                        break
            if last_kw_idx >= 0:
                indent = "    "
                auto_lines = [f'{indent}{AUTO_MARKER}\n']
                auto_lines += [f'{indent}- "{kw}"\n' for kw in synced_keywords]
                for al in reversed(auto_lines):
                    lines.insert(last_kw_idx + 1, al)
            with open(config_path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            print(f"\n🔄 已同步 {len(synced_keywords)} 个兴趣关键词到 config.yaml:")
            for kw in synced_keywords:
                print(f"  + {kw}")
        else:
            # 即使没有新词，也要清理可能残留的旧自动区域
            with open(config_path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            print(f"\n✅ config.yaml 搜索词已覆盖所有正面关键词，无需更新")
    except Exception as e:
        print(f"\n⚠️ 回写 config.yaml 失败: {e}")

    log_activity("update_keywords", {
        "positive_count": profile["positive_count"],
        "negative_count": profile["negative_count"],
        "top_positive": [p["phrase"] for p in profile["positive_phrases"][:5]],
        "synced_keywords": synced_keywords,
    })


# =============================================================================
# fix 模式：修复 fallback 摘要
# =============================================================================

def cmd_fix(config: dict, limit: int = 0):
    """扫描并修复使用 fallback 摘要的论文"""
    ai_cfg = config.get("ai", {})
    output_cfg = config.get("output", {})
    vault_path = config.get("obsidian_vault", "")

    if not vault_path:
        print("错误：请在 config.yaml 中配置 obsidian_vault 路径")
        sys.exit(1)

    folder = output_cfg.get("folder", "papers")

    print("=" * 60)
    print("   论文自动阅读系统 v2 — 修复 fallback 摘要")
    print(f"   AI 后端: {ai_cfg.get('provider', 'codex')} / {ai_cfg.get('model', 'N/A')}")
    print("=" * 60)

    # 找到所有需要修复的论文
    results = check_vault_papers(vault_path, folder)
    to_fix = [r for r in results if r.needs_regenerate]

    if not to_fix:
        print("\n所有论文摘要质量正常，无需修复")
        return

    if limit > 0:
        to_fix = to_fix[:limit]

    print(f"\n找到 {len(to_fix)} 篇需要修复的论文:\n")
    for i, r in enumerate(to_fix, 1):
        issues = "; ".join(r.issues[:2])
        print(f"  {i}. {r.title[:55]}")
        print(f"     问题: {issues}")

    print(f"\n开始修复...\n")

    fixed = 0
    failed = 0
    for i, r in enumerate(to_fix, 1):
        print(f"  [{i}/{len(to_fix)}] {r.title[:55]}...", flush=True)
        try:
            ok = regenerate_summary(r.filepath, config)
            if ok:
                fixed += 1
                print(f"    [OK] 重新生成成功", flush=True)
            else:
                failed += 1
                print(f"    [FAIL] 重新生成失败", flush=True)
        except Exception as e:
            failed += 1
            print(f"    [FAIL] 异常: {e}", flush=True)
        if i < len(to_fix):
            time.sleep(8)

    print(f"\n完成！修复 {fixed}/{len(to_fix)} 篇，失败 {failed} 篇")
    log_activity("fix", {"total": len(to_fix), "fixed": fixed, "failed": failed})


# =============================================================================
# CLI 入口
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="📚 论文自动阅读系统 v2 (Codex/LiteLLM 多后端)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s scan              扫描 arXiv 新论文并写入 Obsidian（含二次检查）
  %(prog)s scan --no-ai      跳过 AI 摘要
  %(prog)s scan --no-check   跳过二次检查
  %(prog)s check             检查全部论文笔记质量
  %(prog)s check --fix       检查并自动修复不合格笔记
  %(prog)s check --recent 10 只检查最近 10 篇
  %(prog)s deep              对所有收藏论文做深度分析
  %(prog)s deep --paper ID   对指定论文做深度分析
  %(prog)s list              查看收藏论文列表
  %(prog)s list --all        查看全部论文
  %(prog)s cleanup           预览 rejected 论文（不删除）
  %(prog)s cleanup --confirm 确认删除 rejected 论文
  %(prog)s cleanup --days 7  仅清理 7 天前创建的论文
  %(prog)s sync-history      将 vault 中所有论文 ID 同步到 history.json
  %(prog)s migrate           迁移旧版 interested 字段为 status
  %(prog)s fix               修复 fallback 摘要论文
  %(prog)s fix --limit 5     只修复前 5 篇
  %(prog)s stats             查看阅读统计
  %(prog)s dashboard         生成 Obsidian 统计仪表板

AI 后端配置 (config.yaml 中的 ai.provider):
  codex    使用本地 Codex CLI（ChatGPT 订阅，默认 gpt-5.4）
  litellm  使用 LiteLLM（支持 100+ 模型提供商）
  raw      直接调用 OpenAI 兼容 API（兼容原版）
""",
    )
    parser.add_argument(
        "--config", "-c",
        default=str(DEFAULT_CONFIG),
        help="配置文件路径 (默认: paper_reader_v2/config.yaml)",
    )

    subparsers = parser.add_subparsers(dest="command", help="运行模式")

    scan_parser = subparsers.add_parser("scan", help="扫描 arXiv 新论文（含二次检查）")
    scan_parser.add_argument("--no-ai", action="store_true", help="跳过 AI 摘要")
    scan_parser.add_argument("--no-check", action="store_true", help="跳过二次质量检查")
    scan_parser.add_argument("--dry-run", action="store_true", help="仅抓取，不写入文件")

    check_parser = subparsers.add_parser("check", help="检查论文笔记质量")
    check_parser.add_argument("--fix", action="store_true", help="自动修复不合格笔记")
    check_parser.add_argument("--recent", "-n", type=int, default=0, help="只检查最近 N 篇")

    deep_parser = subparsers.add_parser("deep", help="深度分析收藏论文")
    deep_parser.add_argument("--paper", "-p", default="", help="指定 arXiv ID 分析")
    deep_parser.add_argument("--force", "-f", action="store_true", help="强制重新分析")

    list_parser = subparsers.add_parser("list", help="查看论文列表")
    list_parser.add_argument("--all", "-a", action="store_true", dest="show_all", help="显示所有论文")

    cleanup_parser = subparsers.add_parser("cleanup", help="清理 rejected 状态的论文")
    cleanup_parser.add_argument("--confirm", action="store_true", help="确认执行删除（默认仅预览）")
    cleanup_parser.add_argument("--days", "-d", type=int, default=3, help="仅清理创建超过 N 天的论文 (默认: 3)")

    subparsers.add_parser("sync-history", help="将 vault 论文 ID 同步到 history.json")

    subparsers.add_parser("migrate", help="迁移旧版 interested 字段为 status")

    fix_parser = subparsers.add_parser("fix", help="修复 fallback 摘要论文")
    fix_parser.add_argument("--limit", "-l", type=int, default=0, help="最多修复 N 篇 (默认: 全部)")

    subparsers.add_parser("stats", help="查看阅读统计")

    subparsers.add_parser("dashboard", help="生成 Obsidian 统计仪表板")

    subparsers.add_parser("update-keywords", help="更新正反关键词画像")

    args = parser.parse_args()
    config = load_config(args.config)

    command = args.command or "scan"

    if command == "scan":
        no_ai = getattr(args, "no_ai", False)
        dry_run = getattr(args, "dry_run", False)
        no_check = getattr(args, "no_check", False)
        cmd_scan(config, no_ai=no_ai, dry_run=dry_run, no_check=no_check)
    elif command == "check":
        fix = getattr(args, "fix", False)
        recent = getattr(args, "recent", 0)
        cmd_check(config, fix=fix, recent=recent)
    elif command == "deep":
        paper_id = getattr(args, "paper", "")
        force = getattr(args, "force", False)
        cmd_deep(config, paper_id=paper_id, force=force)
    elif command == "list":
        show_all = getattr(args, "show_all", False)
        cmd_list(config, show_all=show_all)
    elif command == "cleanup":
        days = getattr(args, "days", 3)
        confirm = getattr(args, "confirm", False)
        cmd_cleanup(config, days=days, confirm=confirm)
    elif command == "sync-history":
        cmd_sync_history(config)
    elif command == "migrate":
        cmd_migrate(config)
    elif command == "fix":
        limit = getattr(args, "limit", 0)
        cmd_fix(config, limit=limit)
    elif command == "stats":
        cmd_stats(config)
    elif command == "dashboard":
        cmd_dashboard(config)
    elif command == "update-keywords":
        cmd_update_keywords(config)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⏹️ 用户中断")
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"\n❌ 未捕获异常: {type(e).__name__}: {e}")
        sys.exit(1)
