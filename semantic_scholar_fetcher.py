"""Semantic Scholar 论文抓取模块 - 从 Semantic Scholar API 获取论文"""

import time
from datetime import datetime, timedelta, timezone

import requests as _requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .arxiv_fetcher import Paper

S2_API_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
S2_FIELDS = "paperId,title,abstract,authors,year,citationCount,url,externalIds,publicationDate,venue"

# Semantic Scholar 速率限制: 1 req/s（无 API key）
_REQUEST_INTERVAL = 5.0  # S2 无 API key 限制严格，加大间隔


def fetch_papers_s2(
    keywords: list[str],
    max_results: int = 20,
    days_back: int = 30,
    min_citation_count: int = 0,
) -> list[Paper]:
    """从 Semantic Scholar 搜索论文

    Args:
        keywords: 搜索关键词列表
        max_results: 最大返回数量
        days_back: 只获取最近 N 天的论文
        min_citation_count: 最低引用数过滤

    Returns:
        Paper 对象列表（复用 arxiv_fetcher.Paper）
    """
    session = _requests.Session()
    session.trust_env = False  # 绕过系统代理
    retries = Retry(total=2, backoff_factor=3,
                    status_forcelist=[500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retries))

    # 先探测 S2 是否可用（轻量请求）
    try:
        probe = session.get(S2_API_URL, params={"query": "test", "limit": 1, "fields": "paperId"}, timeout=10)
        if probe.status_code == 429:
            print("  [S2] API 当前限流中，跳过 Semantic Scholar")
            return []
    except Exception:
        print("  [S2] API 无法连接，跳过 Semantic Scholar")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    all_papers: dict[str, Paper] = {}

    # 将关键词分组查询（较大分组减少请求次数，避免 S2 限流）
    query_groups = _build_query_groups(keywords, group_size=6)
    # 最多发 5 个查询，避免触发 429
    query_groups = query_groups[:5]

    for i, query in enumerate(query_groups):
        if i > 0:
            time.sleep(_REQUEST_INTERVAL)

        try:
            papers = _search_one_query(session, query, max_results, cutoff, min_citation_count)
            for p in papers:
                if p.arxiv_id not in all_papers:
                    all_papers[p.arxiv_id] = p
        except _requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                print(f"  [S2] 触发限流 (429)，停止后续查询")
                break
            print(f"  [S2] 查询失败 ({query[:40]}...): {e}")
        except Exception as e:
            print(f"  [S2] 查询失败 ({query[:40]}...): {e}")

    result = list(all_papers.values())
    result.sort(key=lambda p: p.published, reverse=True)
    return result[:max_results]


def fetch_conference_papers_s2(
    keywords: list[str],
    conferences: list[str],
    max_results: int = 20,
    year: int | None = None,
) -> list[Paper]:
    """从 Semantic Scholar 搜索顶会论文

    通过在查询中附加会议名称来过滤顶会论文。

    Args:
        keywords: 核心搜索关键词
        conferences: 会议名称列表 (如 ["ICLR", "ICML", "NeurIPS"])
        max_results: 最大返回数量
        year: 限制论文年份 (如 2025, 2026)

    Returns:
        Paper 对象列表
    """
    session = _requests.Session()
    session.trust_env = False
    retries = Retry(total=2, backoff_factor=3,
                    status_forcelist=[500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retries))

    # 先探测 S2 是否可用
    try:
        probe = session.get(S2_API_URL, params={"query": "test", "limit": 1, "fields": "paperId"}, timeout=10)
        if probe.status_code == 429:
            print("  [S2-会议] API 当前限流中，跳过顶会搜索")
            return []
    except Exception:
        print("  [S2-会议] API 无法连接，跳过顶会搜索")
        return []

    all_papers: dict[str, Paper] = {}
    # 取前 5 个核心关键词与每个会议组合查询
    core_kws = keywords[:5]

    # 构建查询：每个会议 + 核心关键词组合
    queries = []
    for conf in conferences:
        year_str = str(year) if year else ""
        kw_str = " ".join(core_kws[:3])
        q = f"{kw_str} {conf} {year_str}".strip()
        queries.append((conf, q))

    for i, (conf, query) in enumerate(queries):
        if i > 0:
            time.sleep(_REQUEST_INTERVAL)

        try:
            params = {
                "query": query,
                "limit": min(max_results, 50),
                "fields": S2_FIELDS,
                "year": str(year) if year else "",
            }
            # 移除空参数
            params = {k: v for k, v in params.items() if v}
            resp = session.get(S2_API_URL, params=params, timeout=30)
            if resp.status_code == 429:
                print(f"  [S2-会议] 触发限流 (429)，停止后续查询")
                break
            resp.raise_for_status()
            data = resp.json()

            count = 0
            for item in data.get("data", []):
                paper = _parse_s2_paper(item)
                if paper is None:
                    continue
                # 检查 venue 是否匹配目标会议
                venue = (item.get("venue", "") or "").upper()
                if conf.upper() in venue or conf.upper() in paper.comment.upper():
                    if paper.arxiv_id not in all_papers:
                        all_papers[paper.arxiv_id] = paper
                        count += 1
            if count:
                print(f"  [S2-会议] {conf}: 找到 {count} 篇论文")
        except _requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                print(f"  [S2-会议] 触发限流 (429)，停止后续查询")
                break
            print(f"  [S2-会议] {conf} 查询失败: {e}")
        except Exception as e:
            print(f"  [S2-会议] {conf} 查询失败: {e}")

    result = list(all_papers.values())
    result.sort(key=lambda p: p.published, reverse=True)
    return result[:max_results]


def _search_one_query(
    session: _requests.Session,
    query: str,
    limit: int,
    cutoff: datetime,
    min_citations: int,
) -> list[Paper]:
    """执行单次 Semantic Scholar 搜索"""
    params = {
        "query": query,
        "limit": min(limit, 100),  # S2 API 单次最多 100
        "fields": S2_FIELDS,
    }
    resp = session.get(S2_API_URL, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    papers = []
    for item in data.get("data", []):
        paper = _parse_s2_paper(item)
        if paper is None:
            continue
        if paper.published < cutoff:
            continue
        if min_citations > 0:
            citation_count = item.get("citationCount", 0) or 0
            if citation_count < min_citations:
                continue
        papers.append(paper)

    return papers


def _parse_s2_paper(item: dict) -> Paper | None:
    """将 S2 API 结果转换为 Paper 对象"""
    title = item.get("title", "")
    abstract = item.get("abstract", "")
    if not title or not abstract:
        return None

    # 提取 arXiv ID（优先）或 S2 paper ID 作为唯一标识
    external_ids = item.get("externalIds") or {}
    arxiv_id = external_ids.get("ArXiv", "")
    if not arxiv_id:
        # 无 arXiv ID 时用 S2 的 corpusId 作为备用标识
        corpus_id = external_ids.get("CorpusId", "")
        arxiv_id = f"s2-{corpus_id}" if corpus_id else f"s2-{item.get('paperId', '')}"

    # 解析作者
    authors = []
    for author in item.get("authors", []):
        name = author.get("name", "")
        if name:
            authors.append(name)

    # 解析日期
    pub_date_str = item.get("publicationDate", "")
    published = _parse_date(pub_date_str)

    # 构建 URL
    s2_url = item.get("url", "")
    if external_ids.get("ArXiv"):
        abs_url = f"https://arxiv.org/abs/{external_ids['ArXiv']}"
        pdf_url = f"https://arxiv.org/pdf/{external_ids['ArXiv']}.pdf"
    else:
        abs_url = s2_url
        pdf_url = ""

    # venue 作为 comment
    venue = item.get("venue", "") or ""
    citation_count = item.get("citationCount", 0) or 0
    comment = f"{venue} (citations: {citation_count})" if venue else f"citations: {citation_count}"

    return Paper(
        arxiv_id=arxiv_id,
        title=title,
        authors=authors,
        abstract=abstract,
        categories=[],  # S2 不提供 arXiv 分类
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
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def _build_query_groups(keywords: list[str], group_size: int = 3) -> list[str]:
    """将关键词分组为查询字符串

    Semantic Scholar 搜索支持自然语言查询，
    将相关关键词组合为一个查询效果更好。
    """
    queries = []
    for i in range(0, len(keywords), group_size):
        group = keywords[i:i + group_size]
        queries.append(" ".join(group))
    return queries
