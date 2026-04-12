"""arXiv 论文抓取模块 - 从 arXiv 获取最新论文"""

import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests as _requests  # type: ignore[import-untyped]
from requests.adapters import HTTPAdapter  # type: ignore[import-untyped]
from urllib3.util.retry import Retry  # type: ignore[import-untyped]


@dataclass
class Paper:
    """论文数据结构"""
    arxiv_id: str
    title: str
    authors: list[str]
    abstract: str
    categories: list[str]
    published: datetime
    updated: datetime
    pdf_url: str
    abs_url: str
    primary_category: str
    comment: str = ""  # arXiv comment（常含会议信息）
    summary: Optional[str] = None  # AI 生成的摘要


ARXIV_API_URL = "https://export.arxiv.org/api/query"


def fetch_papers(
    keywords: list[str],
    categories: list[str],
    max_results: int = 5,
    days_back: int = 3,
) -> list[Paper]:
    """从 arXiv API 抓取论文

    Args:
        keywords: 搜索关键词列表
        categories: arXiv 分类列表 (如 cs.CL, cs.LG)
        max_results: 最大返回数量
        days_back: 只获取最近 N 天的论文

    Returns:
        Paper 对象列表
    """
    # arXiv 查询长度有限制，分批查询（每批最多 20 个关键词）
    BATCH_SIZE = 20
    all_papers: dict[str, Paper] = {}  # arxiv_id -> Paper，自动去重

    session = _requests.Session()
    session.trust_env = False  # 绕过系统代理
    retries = Retry(total=3, backoff_factor=2,
                    status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.mount("http://", HTTPAdapter(max_retries=retries))

    for batch_start in range(0, len(keywords), BATCH_SIZE):
        batch_kws = keywords[batch_start:batch_start + BATCH_SIZE]
        query = _build_query(batch_kws, categories)
        params = {
            'search_query': query,
            'start': 0,
            'max_results': min(max_results * 2, 500),
            'sortBy': 'submittedDate',
            'sortOrder': 'descending',
        }

        try:
            resp = session.get(ARXIV_API_URL, params=params, timeout=90)
            resp.raise_for_status()
            batch_papers = _parse_response(resp.content)
            for p in batch_papers:
                if p.arxiv_id not in all_papers:
                    all_papers[p.arxiv_id] = p
        except Exception as e:
            print(f"  [arXiv] 批次 {batch_start//BATCH_SIZE + 1} 请求失败: {e}")

        # 多批次间礼貌延迟，避免被 arXiv 限流
        if batch_start + BATCH_SIZE < len(keywords):
            import time
            time.sleep(3)

    papers = list(all_papers.values())

    # 按日期过滤
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    papers = [p for p in papers if p.published >= cutoff]

    # 按发布日期降序排列
    papers.sort(key=lambda p: p.published, reverse=True)

    return papers[:max_results]


def _build_query(keywords: list[str], categories: list[str]) -> str:
    """构建 arXiv 搜索查询

    使用 OR 连接关键词，AND 连接分类过滤
    """
    # 关键词部分 — 只搜标题和摘要，避免匹配评论/引用等噪音字段
    kw_parts = [f'ti:"{kw}" OR abs:"{kw}"' for kw in keywords]
    kw_query = " OR ".join(kw_parts)

    # 分类部分
    if categories:
        cat_parts = [f"cat:{cat}" for cat in categories]
        cat_query = " OR ".join(cat_parts)
        return f"({kw_query}) AND ({cat_query})"

    return kw_query


def _parse_response(xml_data: bytes) -> list[Paper]:
    """解析 arXiv API 的 Atom XML 响应"""
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "arxiv": "http://arxiv.org/schemas/atom",
    }
    root = ET.fromstring(xml_data)
    papers = []

    for entry in root.findall("atom:entry", ns):
        title_el = entry.find("atom:title", ns)
        summary_el = entry.find("atom:summary", ns)
        published_el = entry.find("atom:published", ns)
        updated_el = entry.find("atom:updated", ns)

        if title_el is None or summary_el is None:
            continue

        title = " ".join(title_el.text.strip().split())
        abstract = " ".join(summary_el.text.strip().split())

        # 解析作者
        authors = []
        for author in entry.findall("atom:author", ns):
            name_el = author.find("atom:name", ns)
            if name_el is not None:
                authors.append(name_el.text.strip())

        # 解析链接
        pdf_url = ""
        abs_url = ""
        for link in entry.findall("atom:link", ns):
            href = link.get("href", "")
            if link.get("title") == "pdf":
                pdf_url = href
            elif link.get("type") == "text/html":
                abs_url = href

        # 解析分类
        categories = []
        primary_category = ""
        for cat in entry.findall("atom:category", ns):
            term = cat.get("term", "")
            if term:
                categories.append(term)
        prim_cat = entry.find("arxiv:primary_category", ns)
        if prim_cat is not None:
            primary_category = prim_cat.get("term", "")

        # 解析 arXiv ID
        id_el = entry.find("atom:id", ns)
        arxiv_id = ""
        if id_el is not None:
            arxiv_id = id_el.text.strip().split("/abs/")[-1]

        # 解析时间
        published = _parse_datetime(published_el.text if published_el is not None else "")
        updated = _parse_datetime(updated_el.text if updated_el is not None else "")

        # 解析 comment（常包含会议信息如 "Accepted at NeurIPS 2024"）
        comment_el = entry.find("arxiv:comment", ns)
        comment = comment_el.text.strip() if comment_el is not None and comment_el.text else ""

        papers.append(Paper(
            arxiv_id=arxiv_id,
            title=title,
            authors=authors,
            abstract=abstract,
            categories=categories,
            published=published,
            updated=updated,
            pdf_url=pdf_url,
            abs_url=abs_url or f"https://arxiv.org/abs/{arxiv_id}",
            primary_category=primary_category,
            comment=comment,
        ))

    return papers


def _parse_datetime(dt_str: str) -> datetime:
    """解析 ISO 8601 格式时间"""
    if not dt_str:
        return datetime.now(timezone.utc)
    dt_str = dt_str.strip()
    # arXiv 返回格式: 2024-01-15T12:00:00Z
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
