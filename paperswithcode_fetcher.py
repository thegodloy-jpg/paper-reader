"""Papers With Code 论文抓取模块 - 从 PapersWithCode API 获取有代码实现的论文"""

import time
from datetime import datetime, timedelta, timezone

import requests as _requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .arxiv_fetcher import Paper

PWC_API_URL = "https://paperswithcode.com/api/v1/papers/"
PWC_SEARCH_URL = "https://paperswithcode.com/api/v1/search/"

_REQUEST_INTERVAL = 1.0


def fetch_papers_pwc(
    keywords: list[str],
    max_results: int = 20,
    days_back: int = 30,
) -> list[Paper]:
    """从 Papers With Code 搜索论文

    Args:
        keywords: 搜索关键词列表
        max_results: 最大返回数量
        days_back: 只获取最近 N 天的论文

    Returns:
        Paper 对象列表（复用 arxiv_fetcher.Paper）
    """
    session = _requests.Session()
    session.trust_env = False  # 绕过系统代理
    retries = Retry(total=3, backoff_factor=2,
                    status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retries))

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    all_papers: dict[str, Paper] = {}

    # 将关键词分组查询（较大分组减少请求数）
    query_groups = _build_query_groups(keywords, group_size=6)
    query_groups = query_groups[:5]  # 最多 5 个查询

    for i, query in enumerate(query_groups):
        if i > 0:
            time.sleep(_REQUEST_INTERVAL)

        try:
            papers = _search_one_query(session, query, max_results, cutoff)
            for p in papers:
                if p.arxiv_id not in all_papers:
                    all_papers[p.arxiv_id] = p
        except Exception as e:
            print(f"  [PWC] 查询失败 ({query[:40]}...): {e}")

    result = list(all_papers.values())
    result.sort(key=lambda p: p.published, reverse=True)
    return result[:max_results]


def _search_one_query(
    session: _requests.Session,
    query: str,
    limit: int,
    cutoff: datetime,
) -> list[Paper]:
    """执行单次 PapersWithCode 搜索"""
    params = {
        "q": query,
        "page": 1,
        "items_per_page": min(limit, 50),
    }
    resp = session.get(PWC_API_URL, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    papers = []
    for item in data.get("results", []):
        paper = _parse_pwc_paper(item)
        if paper is None:
            continue
        if paper.published < cutoff:
            continue
        papers.append(paper)

    return papers


def _parse_pwc_paper(item: dict) -> Paper | None:
    """将 PWC API 结果转换为 Paper 对象"""
    title = item.get("title", "")
    abstract = item.get("abstract", "")
    if not title or not abstract:
        return None

    # 提取 arXiv ID（PWC 中大部分论文都有 arXiv 链接）
    url_abs = item.get("url_abs", "") or ""
    arxiv_id = ""
    if "arxiv.org/abs/" in url_abs:
        arxiv_id = url_abs.split("/abs/")[-1].strip("/")
    if not arxiv_id:
        # 用 PWC 的 paper id 作为备用标识
        pwc_id = item.get("id", "") or item.get("paper_url", "")
        arxiv_id = f"pwc-{pwc_id}" if pwc_id else ""
    if not arxiv_id:
        return None

    # 解析作者
    authors_raw = item.get("authors", []) or []
    authors = [a if isinstance(a, str) else a.get("name", "") for a in authors_raw]
    authors = [a for a in authors if a]

    # 解析日期
    pub_date_str = item.get("published", "") or item.get("date", "")
    published = _parse_date(pub_date_str)

    # URL
    pdf_url = item.get("url_pdf", "") or ""
    abs_url = url_abs or item.get("paper_url", "") or ""

    # 附加信息（会议、代码仓库数量）
    proceeding = item.get("proceeding", "") or ""
    comment = f"[PWC] {proceeding}" if proceeding else "[PWC]"

    return Paper(
        arxiv_id=arxiv_id,
        title=title,
        authors=authors,
        abstract=abstract,
        categories=[],
        published=published,
        updated=published,
        pdf_url=pdf_url,
        abs_url=abs_url,
        primary_category="",
        comment=comment,
    )


def _parse_date(date_str: str) -> datetime:
    """解析日期字符串"""
    if not date_str:
        return datetime.now(timezone.utc)
    # PWC 日期格式可能是 "2024-01-15" 或 ISO 格式
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime.now(timezone.utc)


def _build_query_groups(keywords: list[str], group_size: int = 3) -> list[str]:
    """将关键词分组为查询字符串"""
    queries = []
    for i in range(0, len(keywords), group_size):
        group = keywords[i:i + group_size]
        queries.append(" ".join(group))
    return queries
