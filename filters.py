"""论文去重、代码仓库检测与优先级排序模块"""

import json
import os
import re
import shutil
import tempfile
import urllib.request
from pathlib import Path
from typing import Optional

import yaml  # type: ignore[import-untyped]

from .arxiv_fetcher import Paper


HISTORY_FILE = Path(__file__).parent / "history.json"

# 顶会名称映射（用于从 arXiv comment 中检测）
TOP_CONFERENCES = [
    "NeurIPS", "NIPS",
    "ICLR",
    "ICML",
    "AAAI",
    "IJCAI",
    "ACL", "EMNLP", "NAACL",
    "CVPR", "ICCV", "ECCV",
    "KDD",
    "SIGIR",
    "OSDI", "SOSP", "MLSys", "ATC",
    "ASPLOS", "ISCA", "MICRO",
]


# --- 去重 ---

def load_history() -> set[str]:
    """加载已处理的论文 arXiv ID 集合"""
    if not HISTORY_FILE.exists():
        return set()
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return set(data.get("processed_ids", []))


def save_history(processed_ids: set[str]):
    """保存已处理的论文 ID（写入前自动备份 + 原子写入防损坏）"""
    # 自动备份
    if HISTORY_FILE.exists():
        shutil.copy2(HISTORY_FILE, HISTORY_FILE.with_suffix(".json.bak"))

    # 原子写入：先写临时文件，再 rename
    fd, tmp_path = tempfile.mkstemp(
        dir=HISTORY_FILE.parent, suffix=".tmp", prefix="history_"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"processed_ids": sorted(processed_ids)}, f, indent=2)
        os.replace(tmp_path, HISTORY_FILE)
    except BaseException:
        # 写入失败时清理临时文件
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def filter_duplicates(papers: list[Paper]) -> list[Paper]:
    """过滤已处理过的论文"""
    history = load_history()
    new_papers = []
    for p in papers:
        # 去掉版本号比较 (如 2604.02047v1 -> 2604.02047)
        base_id = re.sub(r'v\d+$', '', p.arxiv_id)
        if base_id not in history and p.arxiv_id not in history:
            new_papers.append(p)
    skipped = len(papers) - len(new_papers)
    if skipped:
        print(f"  [去重] 跳过 {skipped} 篇已处理论文")
    return new_papers


def mark_processed(papers: list[Paper]):
    """将论文标记为已处理"""
    history = load_history()
    for p in papers:
        base_id = re.sub(r'v\d+$', '', p.arxiv_id)
        history.add(base_id)
    save_history(history)


def mark_ids_processed(arxiv_ids: list[str]):
    """将指定的 arXiv ID 列表标记为已处理"""
    history = load_history()
    for aid in arxiv_ids:
        base_id = re.sub(r'v\d+$', '', aid)
        history.add(base_id)
    save_history(history)


def sync_vault_to_history(vault_path: str, folder: str = "papers") -> int:
    """将 vault 中所有论文的 arxiv_id 同步到 history.json

    Returns:
        新增的 ID 数量
    """
    history = load_history()
    old_count = len(history)

    papers_dir = os.path.join(vault_path, folder)
    if not os.path.exists(papers_dir):
        return 0

    for root, _, files in os.walk(papers_dir):
        for f in files:
            if not f.endswith(".md"):
                continue
            filepath = os.path.join(root, f)
            try:
                with open(filepath, "r", encoding="utf-8") as fh:
                    content = fh.read(2000)  # frontmatter only
                if not content.startswith("---"):
                    continue
                end = content.find("---", 3)
                if end == -1:
                    continue
                fm = yaml.safe_load(content[3:end])
                if fm and fm.get("arxiv_id"):
                    base_id = re.sub(r'v\d+$', '', fm["arxiv_id"])
                    history.add(base_id)
            except Exception:
                continue

    save_history(history)
    return len(history) - old_count


# --- 代码仓库检测 ---

def find_code_url(paper: Paper) -> Optional[str]:
    """尝试从论文 abstract 或 Papers With Code 找到代码仓库

    Returns:
        GitHub/GitLab URL，未找到返回 None
    """
    # 1. 从 abstract 中提取 GitHub 链接
    github_pattern = r'https?://github\.com/[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+'
    matches = re.findall(github_pattern, paper.abstract)
    if matches:
        return matches[0]

    # 2. 查询 Papers With Code API
    try:
        arxiv_id = re.sub(r'v\d+$', '', paper.arxiv_id)
        url = f"https://paperswithcode.com/api/v1/papers/?arxiv_id={arxiv_id}"
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "User-Agent": "paper-reader-bot/1.0",
        })
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            if data.get("results"):
                pwc_id = data["results"][0].get("id")
                if pwc_id:
                    # 获取代码仓库
                    repo_url = f"https://paperswithcode.com/api/v1/papers/{pwc_id}/repositories/"
                    repo_req = urllib.request.Request(repo_url, headers={
                        "Accept": "application/json",
                        "User-Agent": "paper-reader-bot/1.0",
                    })
                    with urllib.request.urlopen(repo_req, timeout=5) as repo_resp:
                        repos = json.loads(repo_resp.read())
                        if repos.get("results"):
                            return repos["results"][0].get("url")
    except BaseException:
        pass

    return None


def _derive_core_keywords(categories_cfg=None):
    """从分类配置中派生核心关键词列表"""
    if categories_cfg:
        keywords = []
        for cat in categories_cfg:
            keywords.extend(cat.get("keywords", []))
        return keywords
    # 默认核心关键词（当 config 中没有 categories 时使用）
    return [
        "speculative decoding", "kv cache", "flash attention",
        "paged attention", "continuous batching", "tensor parallel",
        "operator fusion", "inference optimization", "inference acceleration",
        "quantization", "gptq", "awq", "int4", "int8",
        "pruning", "distillation", "svd", "low-rank",
        "model compression", "mixed-precision",
        "vllm", "trt-llm", "tensorrt", "llm serving",
        "token generation", "throughput", "latency",
    ]


def filter_by_code(papers: list[Paper], prefer_with_code: bool, categories_cfg=None) -> list[Paper]:
    """根据是否有代码仓库过滤论文

    有代码的论文全部保留，无代码的通过关键词相关性判断是否保留。
    """
    if not prefer_with_code:
        return papers

    print(f"  [代码检测] 正在检查 {len(papers)} 篇论文的代码仓库...")
    result = []

    core_keywords = _derive_core_keywords(categories_cfg)

    for paper in papers:
        code_url = find_code_url(paper)
        if code_url:
            paper._code_url = code_url
            result.append(paper)
            print(f"    ✅ {paper.title[:50]}... → {code_url}")
        else:
            # 检查是否高度相关
            text = (paper.title + " " + paper.abstract).lower()
            is_core = any(kw in text for kw in core_keywords)
            if is_core:
                paper._code_url = None
                result.append(paper)
                print(f"    📌 {paper.title[:50]}... (核心相关，无代码)")
            else:
                print(f"    ⏭️  {paper.title[:50]}... (无代码，跳过)")

    print(f"  [代码检测] 保留 {len(result)}/{len(papers)} 篇")
    return result


# --- 顶会检测 ---

def detect_conference(paper: Paper) -> Optional[str]:
    """从论文 comment 中检测顶会名称

    Returns:
        匹配到的会议名称，未检测到返回 None
    """
    text = getattr(paper, 'comment', '') or ''
    if not text:
        return None
    # 匹配模式：Accepted at/to/by CONF 或 Published at/in CONF 或单独的 CONF 2024
    for conf in TOP_CONFERENCES:
        pattern = rf'\b{re.escape(conf)}\b'
        if re.search(pattern, text, re.IGNORECASE):
            return conf
    return None


def sort_by_priority(papers: list[Paper], feedback_profile: dict = None) -> list[Paper]:
    """按优先级排序：有代码+顶会 > 有代码 > 顶会 > 反馈相关性 > 时间倒序

    Args:
        feedback_profile: 用户反馈画像（来自 interest_tracker.build_feedback_profile）
    """
    from .interest_tracker import score_paper_relevance
    from .obsidian_writer import classify_paper

    for p in papers:
        p._conference = detect_conference(p)
        if feedback_profile:
            cat_info = classify_paper(p)
            p._relevance = score_paper_relevance(p.title, cat_info[0], feedback_profile)
        else:
            p._relevance = 0.0

    papers.sort(key=lambda p: (
        (1 if getattr(p, '_code_url', None) else 0),
        (1 if getattr(p, '_conference', None) else 0),
        getattr(p, '_relevance', 0.0),
        p.published,
    ), reverse=True)
    return papers


# =============================================================================
# 活动日志
# =============================================================================

ACTIVITY_LOG_FILE = Path(__file__).parent / "activity_log.json"


def log_activity(action: str, details: dict):
    """记录一条活动日志

    Args:
        action: 操作类型 (scan/fix/deep/cleanup/status_change/dashboard)
        details: 操作详情 dict
    """
    from datetime import datetime

    log = _load_activity_log()
    today = datetime.now().strftime("%Y-%m-%d")
    entry = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "action": action,
        **details,
    }

    if today not in log:
        log[today] = []
    log[today].append(entry)

    # 只保留最近 90 天
    all_dates = sorted(log.keys())
    if len(all_dates) > 90:
        for old_date in all_dates[:-90]:
            del log[old_date]

    _save_activity_log(log)


def _load_activity_log() -> dict:
    if not ACTIVITY_LOG_FILE.exists():
        return {}
    with open(ACTIVITY_LOG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_activity_log(log: dict):
    with open(ACTIVITY_LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


def get_activity_log(days: int = 30) -> dict:
    """获取最近N天的活动日志"""
    from datetime import datetime, timedelta
    log = _load_activity_log()
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    return {d: entries for d, entries in log.items() if d >= cutoff}
