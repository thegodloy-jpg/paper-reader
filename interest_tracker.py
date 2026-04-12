"""兴趣驱动关键词扩展模块

分析用户标记为 interested 的论文，提取高频术语，
动态补充 arXiv 搜索关键词，让系统越用越懂你的研究方向。
"""

import os
import re
from collections import Counter
from typing import Optional

import yaml  # type: ignore[import-untyped]


# 停用词（通用学术用语 + 常见无意义词）
STOP_WORDS = {
    # 英文停用词
    "a", "an", "the", "of", "for", "and", "in", "on", "to", "with", "via",
    "by", "is", "are", "was", "were", "be", "been", "being", "from", "as",
    "at", "or", "not", "no", "but", "this", "that", "these", "those",
    "it", "its", "we", "our", "they", "their", "which", "what", "how",
    "can", "do", "does", "did", "has", "have", "had", "will", "would",
    "should", "could", "may", "might", "shall", "more", "most", "very",
    "also", "than", "then", "so", "if", "when", "where", "while",
    "about", "into", "through", "during", "before", "after", "above",
    "between", "each", "all", "both", "such", "other", "only", "same",
    "over", "under", "up", "down", "out", "new", "used", "using",
    # 常见学术通用词（不含特定领域信息）
    "model", "models", "method", "methods", "approach", "paper", "results",
    "based", "proposed", "performance", "show", "shows", "shown",
    "achieve", "achieves", "achieved", "demonstrate", "demonstrates",
    "improve", "improves", "improved", "improvement", "existing",
    "large", "language", "learning", "neural", "network", "networks",
    "deep", "training", "trained", "dataset", "data", "task", "tasks",
    "compared", "comparison", "evaluation", "experimental", "experiments",
    "state-of-the-art", "sota", "baseline", "baselines",
    "figure", "table", "section", "work", "recent", "different",
    "several", "various", "significant", "significantly", "effectively",
    "respectively", "without", "across", "further", "first", "one", "two",
    # URL / 链接 / markdown / LaTeX 残留词
    "http", "https", "www", "github", "com", "org", "arxiv", "abs",
    "pdf", "html", "fig", "ref", "cite", "mathbf", "mathrm", "mathcal",
    "textbf", "text", "frac", "left", "right", "times", "cdot", "sum",
    "log", "exp", "min", "max", "arg", "sup", "inf",
    "prism", "alpha", "beta", "gamma", "delta", "lambda", "theta",
    # 额外学术通用词
    "input", "output", "outputs", "inputs", "layer", "layers",
    "token", "tokens", "attention", "number", "parameters", "parameter",
    "accuracy", "efficiency", "efficient", "framework", "architecture",
    "generation", "pre-trained", "pretrained", "fine-tuning", "fine-tuned",
    # 通用硬件/系统词（会匹配大量无关论文）
    "gpu", "gpus", "cpu", "cpus", "memory", "bandwidth", "latency",
    "throughput", "hardware", "accelerator", "chip", "device", "devices",
    "server", "servers", "cluster", "node", "nodes", "compute", "computing",
    "system", "systems", "platform", "resource", "resources",
    # 通用 ML 词
    "llm", "llms", "transformer", "transformers", "inference", "serving",
    "optimization", "optimized", "parallel", "distributed", "training",
    "benchmark", "benchmarks", "evaluation", "implementation",
}


def extract_interest_keywords(
    vault_path: str,
    folder: str = "papers",
    existing_keywords: Optional[list[str]] = None,
    max_keywords: int = 5,
) -> list[str]:
    """从 interested 论文中提取高频术语作为额外搜索关键词

    Args:
        vault_path: Obsidian vault 路径
        folder: 论文文件夹名
        existing_keywords: 已有的搜索关键词（用于去重）
        max_keywords: 最多返回多少个新关键词

    Returns:
        新发现的关键词列表
    """
    papers_dir = os.path.join(vault_path, folder)
    if not os.path.exists(papers_dir):
        return []

    # 收集 interested 论文的标题和摘要
    titles = []
    abstracts = []

    for root, dirs, files in os.walk(papers_dir):
        for f in files:
            if not f.endswith(".md"):
                continue
            fp = os.path.join(root, f)
            try:
                with open(fp, "r", encoding="utf-8") as fh:
                    content = fh.read()
                if not content.startswith("---"):
                    continue
                end = content.find("---", 3)
                if end == -1:
                    continue
                fm = yaml.safe_load(content[3:end])
                if not fm:
                    continue
                if fm.get("status") != "interested":
                    continue

                title = fm.get("title", "")
                if title:
                    titles.append(title)

                # 提取原始摘要（Abstract 部分）
                abs_match = re.search(
                    r'## (?:原始摘要|摘要).*?\n\n(.+?)(?=\n\n##|\n---|\Z)',
                    content, re.DOTALL
                )
                if abs_match:
                    abstracts.append(abs_match.group(1).strip())
            except Exception:
                continue

    if not titles:
        return []

    # 提取 bigram 和 trigram（论文关键术语通常是多词短语）
    all_text = " ".join(titles + abstracts)
    # 清洗噪音：URL、LaTeX、markdown 链接
    all_text = re.sub(r'https?://\S+', ' ', all_text)  # 去除 URL
    all_text = re.sub(r'\\[a-zA-Z]+\{[^}]*\}', ' ', all_text)  # 去除 \cmd{...}
    all_text = re.sub(r'\\[a-zA-Z]+', ' ', all_text)  # 去除 \cmd
    all_text = re.sub(r'\$[^$]*\$', ' ', all_text)  # 去除 $...$
    all_text = re.sub(r'\[.*?\]\(.*?\)', ' ', all_text)  # 去除 [text](url)
    all_text = re.sub(r'[^a-zA-Z\s-]', ' ', all_text)  # 只保留字母和连字符
    all_text = all_text.lower()
    words = re.findall(r'[a-z][a-z-]+', all_text)
    words = [w for w in words if w not in STOP_WORDS and len(w) > 2]

    # 统计 bigrams
    bigrams = []
    for i in range(len(words) - 1):
        bg = f"{words[i]} {words[i+1]}"
        bigrams.append(bg)

    # 统计 trigrams
    trigrams = []
    for i in range(len(words) - 2):
        tg = f"{words[i]} {words[i+1]} {words[i+2]}"
        trigrams.append(tg)

    # 合并统计，给 trigram 更高权重
    phrase_counts = Counter()
    for bg in bigrams:
        phrase_counts[bg] += 1
    for tg in trigrams:
        phrase_counts[tg] += 2  # trigram 权重更高

    # 单词统计（过滤太常见的）
    word_counts = Counter(words)

    # 构建已有关键词的小写集合用于去重
    existing_lower = set()
    if existing_keywords:
        for kw in existing_keywords:
            existing_lower.add(kw.lower())
            # 也把关键词的各个词加入，避免过于相似的扩展
            for w in kw.lower().split():
                if len(w) > 3:
                    existing_lower.add(w)

    # 筛选新关键词
    new_keywords = []

    def _is_valid_phrase(phrase: str) -> bool:
        """过滤无效短语"""
        parts = phrase.split()
        # 有重复词的短语无效 (如 'llm llm')
        if len(parts) != len(set(parts)):
            return False
        # 太短的短语无效（每个词至少 3 字符）
        if any(len(p) < 3 for p in parts):
            return False
        # 含连字符的截断词无效 (如 'moe-spac')
        for p in parts:
            if "-" in p:
                sub = p.split("-")
                if any(len(s) < 2 for s in sub if s):
                    return False
        # 短语中所有词都是通用词则无效 (如 'gpu llm', 'cpu gpu')
        if all(p in STOP_WORDS for p in parts):
            return False
        return True

    # 先从高频短语中选
    for phrase, count in phrase_counts.most_common(30):
        if count < 2:  # 至少出现 2 次
            continue
        if not _is_valid_phrase(phrase):
            continue
        # 检查是否与已有关键词重复
        if any(phrase in exist or exist in phrase for exist in existing_lower):
            continue
        new_keywords.append(phrase)
        if len(new_keywords) >= max_keywords:
            break

    # 如果短语不够，从高频单词补充
    if len(new_keywords) < max_keywords:
        for word, count in word_counts.most_common(20):
            if count < 3:  # 单词要求出现 3 次以上
                continue
            if word in existing_lower:
                continue
            if any(word in kw for kw in new_keywords):
                continue
            new_keywords.append(word)
            if len(new_keywords) >= max_keywords:
                break

    return new_keywords


def get_interest_summary(vault_path: str, folder: str = "papers") -> dict:
    """获取兴趣论文的统计摘要

    Returns:
        {
            "count": int,
            "categories": {cat: count},
            "keywords": [str],  # 提取的关键词
        }
    """
    papers_dir = os.path.join(vault_path, folder)
    if not os.path.exists(papers_dir):
        return {"count": 0, "categories": {}, "keywords": []}

    count = 0
    cats = {}
    for root, dirs, files in os.walk(papers_dir):
        for f in files:
            if not f.endswith(".md"):
                continue
            fp = os.path.join(root, f)
            try:
                with open(fp, "r", encoding="utf-8") as fh:
                    content = fh.read()
                if not content.startswith("---"):
                    continue
                end = content.find("---", 3)
                if end == -1:
                    continue
                fm = yaml.safe_load(content[3:end])
                if not fm or fm.get("status") != "interested":
                    continue
                count += 1
                cat = fm.get("category", "未分类")
                cats[cat] = cats.get(cat, 0) + 1
            except Exception:
                continue

    return {
        "count": count,
        "categories": cats,
        "keywords": extract_interest_keywords(vault_path, folder),
    }


def build_feedback_profile(vault_path: str, folder: str = "papers") -> dict:
    """从 interested/rejected 论文构建用户偏好画像

    返回:
        {
            "positive_terms": Counter,   # interested 论文中的高频词
            "negative_terms": Counter,   # rejected 论文中的高频词
            "positive_cats": Counter,    # interested 的分类分布
            "negative_cats": Counter,    # rejected 的分类分布
        }
    """
    papers_dir = os.path.join(vault_path, folder)
    if not os.path.exists(papers_dir):
        return {"positive_terms": Counter(), "negative_terms": Counter(),
                "positive_cats": Counter(), "negative_cats": Counter()}

    positive_texts = []
    negative_texts = []
    positive_cats = Counter()
    negative_cats = Counter()

    for root, dirs, files in os.walk(papers_dir):
        for f in files:
            if not f.endswith(".md"):
                continue
            fp = os.path.join(root, f)
            try:
                with open(fp, "r", encoding="utf-8") as fh:
                    content = fh.read()
                if not content.startswith("---"):
                    continue
                end = content.find("---", 3)
                if end == -1:
                    continue
                fm = yaml.safe_load(content[3:end])
                if not fm:
                    continue
                status = fm.get("status", "")
                title = fm.get("title", "")
                cat = fm.get("category", "")
                if status in ("interested", "reading", "done"):
                    positive_texts.append(title)
                    if cat:
                        positive_cats[cat] += 1
                elif status == "rejected":
                    negative_texts.append(title)
                    if cat:
                        negative_cats[cat] += 1
            except Exception:
                continue

    def _extract_terms(texts):
        all_text = " ".join(texts).lower()
        all_text = re.sub(r'[^a-zA-Z\s-]', ' ', all_text)
        words = re.findall(r'[a-z][a-z-]+', all_text)
        words = [w for w in words if w not in STOP_WORDS and len(w) > 2]
        return Counter(words)

    return {
        "positive_terms": _extract_terms(positive_texts),
        "negative_terms": _extract_terms(negative_texts),
        "positive_cats": positive_cats,
        "negative_cats": negative_cats,
    }


def score_paper_relevance(paper_title: str, paper_category: str, profile: dict) -> float:
    """根据反馈画像计算论文相关性分数

    正面词命中 +1，负面词命中 -0.5，分类偏好 ±2
    返回浮点分数（越高越感兴趣）
    """
    title_lower = paper_title.lower()
    words = re.findall(r'[a-z][a-z-]+', title_lower)
    words = [w for w in words if w not in STOP_WORDS and len(w) > 2]

    score = 0.0
    pos_terms = profile.get("positive_terms", Counter())
    neg_terms = profile.get("negative_terms", Counter())

    for w in words:
        if w in pos_terms:
            score += 1.0
        if w in neg_terms:
            score -= 0.5

    # 分类偏好
    pos_cats = profile.get("positive_cats", Counter())
    neg_cats = profile.get("negative_cats", Counter())
    if paper_category:
        if paper_category in pos_cats:
            score += 2.0
        if paper_category in neg_cats:
            score -= 1.0

    return score


# =============================================================================
# 正反关键词画像（短语级别）
# =============================================================================

_PROFILE_FILE = os.path.join(os.path.dirname(__file__), "keyword_profile.json")


def _extract_phrases(texts: list[str], min_count: int = 2, max_phrases: int = 20) -> list[dict]:
    """从文本列表中提取高频 bigram/trigram 短语

    Returns:
        [{"phrase": str, "count": int}, ...]  按频率降序
    """
    import json
    all_text = " ".join(texts).lower()
    all_text = re.sub(r'https?://\S+', ' ', all_text)
    all_text = re.sub(r'\\[a-zA-Z]+\{[^}]*\}', ' ', all_text)
    all_text = re.sub(r'\\[a-zA-Z]+', ' ', all_text)
    all_text = re.sub(r'\$[^$]*\$', ' ', all_text)
    all_text = re.sub(r'\[.*?\]\(.*?\)', ' ', all_text)
    all_text = re.sub(r'[^a-zA-Z\s-]', ' ', all_text)
    words = re.findall(r'[a-z][a-z-]+', all_text)
    words = [w for w in words if w not in STOP_WORDS and len(w) > 2]

    phrase_counts = Counter()
    for i in range(len(words) - 1):
        phrase_counts[f"{words[i]} {words[i+1]}"] += 1
    for i in range(len(words) - 2):
        phrase_counts[f"{words[i]} {words[i+1]} {words[i+2]}"] += 2

    # 单词也纳入
    word_counts = Counter(words)

    results = []
    seen_words = set()
    for phrase, count in phrase_counts.most_common(50):
        if count < min_count:
            break
        parts = phrase.split()
        if len(parts) != len(set(parts)):
            continue
        if any(len(p) < 3 for p in parts):
            continue
        results.append({"phrase": phrase, "count": count})
        seen_words.update(parts)
        if len(results) >= max_phrases:
            break

    # 补充高频单词
    if len(results) < max_phrases:
        for word, count in word_counts.most_common(30):
            if count < 3 or word in seen_words:
                continue
            results.append({"phrase": word, "count": count})
            if len(results) >= max_phrases:
                break

    return results


def build_keyword_profile(vault_path: str, folder: str = "papers") -> dict:
    """构建正反关键词画像（短语级别），返回并持久化到 keyword_profile.json

    Returns:
        {
            "updated": "2026-04-06 01:00",
            "positive_count": int,   # interested 论文数
            "negative_count": int,   # rejected 论文数
            "positive_phrases": [{"phrase": str, "count": int}, ...],
            "negative_phrases": [{"phrase": str, "count": int}, ...],
            "positive_categories": {"cat": count, ...},
            "negative_categories": {"cat": count, ...},
        }
    """
    import json
    from datetime import datetime

    papers_dir = os.path.join(vault_path, folder)
    if not os.path.exists(papers_dir):
        return {}

    positive_texts = []
    negative_texts = []
    positive_cats = Counter()
    negative_cats = Counter()

    for root, dirs, files in os.walk(papers_dir):
        for f in files:
            if not f.endswith(".md"):
                continue
            fp = os.path.join(root, f)
            try:
                with open(fp, "r", encoding="utf-8") as fh:
                    content = fh.read()
                if not content.startswith("---"):
                    continue
                end = content.find("---", 3)
                if end == -1:
                    continue
                fm = yaml.safe_load(content[3:end])
                if not fm:
                    continue
                status = fm.get("status", "")
                title = fm.get("title", "")
                cat = fm.get("category", "")

                # 同时收集标题和摘要翻译（更丰富的信号）
                body_text = title
                abs_match = re.search(
                    r'## (?:原始摘要|摘要).*?\n(.*?)(?=\n## |\Z)',
                    content, re.DOTALL
                )
                if abs_match:
                    body_text += " " + abs_match.group(1).strip()

                if status in ("interested", "reading", "done"):
                    positive_texts.append(body_text)
                    if cat:
                        positive_cats[cat] += 1
                elif status == "rejected":
                    negative_texts.append(body_text)
                    if cat:
                        negative_cats[cat] += 1
            except Exception:
                continue

    positive_raw = _extract_phrases(positive_texts, max_phrases=40)
    negative_raw = _extract_phrases(negative_texts, max_phrases=40)

    # 构建差分：计算净倾向分数，消除正反重叠
    pos_map = {p["phrase"]: p["count"] for p in positive_raw}
    neg_map = {p["phrase"]: p["count"] for p in negative_raw}
    all_phrases = set(pos_map.keys()) | set(neg_map.keys())

    net_positive = []
    net_negative = []
    for phrase in all_phrases:
        pc = pos_map.get(phrase, 0)
        nc = neg_map.get(phrase, 0)
        # 正面论文更多时需归一化：按论文数量加权
        if len(positive_texts) > 0 and len(negative_texts) > 0:
            pos_rate = pc / len(positive_texts)
            neg_rate = nc / len(negative_texts)
        else:
            pos_rate = pc
            neg_rate = nc

        diff = pos_rate - neg_rate
        if diff > 0.05:  # 明显偏正
            net_positive.append({"phrase": phrase, "count": pc, "net": round(diff, 3)})
        elif diff < -0.05:  # 明显偏负
            net_negative.append({"phrase": phrase, "count": nc, "net": round(abs(diff), 3)})
        # |diff| <= 0.05 的为中性词，两边都不放

    net_positive.sort(key=lambda x: x["net"], reverse=True)
    net_negative.sort(key=lambda x: x["net"], reverse=True)

    profile = {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "positive_count": len(positive_texts),
        "negative_count": len(negative_texts),
        "positive_phrases": net_positive[:20],
        "negative_phrases": net_negative[:20],
        "positive_categories": dict(positive_cats.most_common()),
        "negative_categories": dict(negative_cats.most_common()),
    }

    # 持久化
    with open(_PROFILE_FILE, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)

    return profile


def load_keyword_profile() -> dict:
    """读取已持久化的关键词画像"""
    import json
    if not os.path.exists(_PROFILE_FILE):
        return {}
    with open(_PROFILE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)
