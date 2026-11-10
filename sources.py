#!/usr/bin/python3
"""
sources.py — GitHub Trending & Hacker News 早报的数据抓取层

包含两个核心数据源：
1. GitHub Trending（https://github.com/trending?since=daily 或 weekly）
   - 解析榜单前 10 个仓库（owner/repo、简介、语言、总 Star、总 Fork、今日/本周新增 Star）
   - 并发拉取各仓库 README 核心段落（自动过滤徽章图片与 HTML 噪音），供写稿模型准确理解项目定位与架构亮点。
2. Hacker News 热榜前五（Algolia HN API + 原文正文提取 + 评论区高赞交锋）
   - 结合过去 30 小时高分帖与当前 Frontpage 热帖，按 Points 排序取前 5 名（若前 5 中有重复在榜旧帖，额外附带第 6–7 名备选）
   - 并发抓取外链正文摘要（复用 shared/bot_utils.fetch_article_text）及 HN 评论区前 4 条核心评论。
"""

from __future__ import annotations

import html
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
from bot_utils import fetch_article_text
from bot_utils import url_key as _base_url_key


def url_key(url: str) -> str:
    """去重键。2026-11-10 修复：共享层 url_key 会去掉 query，导致所有
    news.ycombinator.com/item?id=XXX 讨论串都塌缩成同一个键，任何 HN 帖子都被误判
    为「重复在榜」。HN 讨论串必须保留 id 参数；其余 URL 沿用共享层归一化规则。"""
    if not url:
        return ""
    m = re.search(r"news\.ycombinator\.com/item\?id=(\d+)", url)
    if m:
        return f"https://news.ycombinator.com/item?id={m.group(1)}"
    return _base_url_key(url)


_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_ARTICLE_RE = re.compile(r'<article\s+class="Box-row"[^>]*>(.*?)</article>', re.DOTALL)
_H2_LINK_RE = re.compile(r'<h2\s+class="h3\s+lh-condensed"[^>]*>.*?<a\s+[^>]*href="(/[^"]+)"', re.DOTALL)
_DESC_RE = re.compile(r'<p\s+class="col-9[^"]*"[^>]*>(.*?)</p>', re.DOTALL)
_LANG_RE = re.compile(r'itemprop="programmingLanguage"[^>]*>(.*?)</span>', re.DOTALL)
_STARGAZERS_RE = re.compile(r'href="/[^/]+/[^/]+/stargazers"[^>]*>(.*?)</a>', re.DOTALL)
_FORKS_RE = re.compile(r'href="/[^/]+/[^/]+/forks"[^>]*>(.*?)</a>', re.DOTALL)
_PERIOD_STARS_RE = re.compile(r'class="d-inline-block\s+float-sm-right"[^>]*>(.*?)</span>', re.DOTALL)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean_text(raw: str) -> str:
    if not raw:
        return ""
    txt = _TAG_STRIP_RE.sub(" ", raw)
    return _WS_RE.sub(" ", html.unescape(txt)).strip()


def _clean_readme(md: str, max_chars: int = 1000) -> str:
    """清洗 README.md，去掉徽章、HTML 块、纯链接行，保留核心文字说明。"""
    if not md:
        return ""
    # 去掉 HTML 注释和常见居中小部件/图片标签
    md = re.sub(r"<!--.*?-->", " ", md, flags=re.DOTALL)
    md = re.sub(r"<(p|div|table|picture|img|a|br|hr|h[1-6]|span|sup|sub)\b[^>]*>", " ", md, flags=re.IGNORECASE)
    md = re.sub(r"</(p|div|table|picture|a|h[1-6]|span|sup|sub)>", " ", md, flags=re.IGNORECASE)
    lines = []
    for raw_line in md.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # 跳过纯徽章/图片行
        if line.startswith("[![") or line.startswith("![") or line.startswith("<img"):
            continue
        # 跳过纯分隔线或表格对齐行
        if re.match(r"^[\-\=\|\:\s]+$", line):
            continue
        # 将 Markdown 链接 [label](url) 简化为 label
        line = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", line)
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
        line = _WS_RE.sub(" ", line).strip()
        if len(line) >= 8:
            lines.append(line)
        if sum(len(x) for x in lines) >= max_chars * 1.3:
            break
    joined = " / ".join(lines)
    return joined[:max_chars].strip()


def _fetch_repo_readme(repo: str, timeout: int = 8) -> str:
    """尝试拉取仓库的 README 摘要（依次尝试 HEAD/README.md 与 HEAD/readme.md）。"""
    for filename in ("README.md", "readme.md", "README.rst"):
        url = f"https://raw.githubusercontent.com/{repo}/HEAD/{filename}"
        try:
            resp = requests.get(url, headers={"User-Agent": _UA}, timeout=timeout)
            if resp.status_code == 200 and resp.text:
                cleaned = _clean_readme(resp.text)
                if cleaned:
                    return cleaned
        except Exception:
            continue
    return ""


def fetch_github_trending(since: str = "daily", limit: int = 10,
                          seen_history: dict[str, str] | None = None) -> dict[str, Any]:
    """抓取 GitHub Trending（daily 或 weekly）并并发补充 README 摘要。"""
    seen_history = seen_history or {}
    url = f"https://github.com/trending?since={since}"
    resp = requests.get(url, headers={"User-Agent": _UA}, timeout=25)
    resp.raise_for_status()
    raw_html = resp.text

    articles = _ARTICLE_RE.findall(raw_html)
    repos: list[dict[str, Any]] = []

    for idx, block in enumerate(articles[:limit], 1):
        m_link = _H2_LINK_RE.search(block)
        if not m_link:
            continue
        rel_path = m_link.group(1).strip().strip("/")
        # 过滤掉偶发的 sponsor 卡片或非 owner/repo 路径
        parts = [p.strip() for p in rel_path.split("/") if p.strip()]
        if len(parts) != 2:
            continue
        repo_name = f"{parts[0]}/{parts[1]}"
        repo_url = f"https://github.com/{repo_name}"

        m_desc = _DESC_RE.search(block)
        desc = _clean_text(m_desc.group(1)) if m_desc else ""

        m_lang = _LANG_RE.search(block)
        lang = _clean_text(m_lang.group(1)) if m_lang else "Unknown"

        m_stars = _STARGAZERS_RE.search(block)
        total_stars = _clean_text(m_stars.group(1)) if m_stars else ""

        m_forks = _FORKS_RE.search(block)
        forks = _clean_text(m_forks.group(1)) if m_forks else ""

        m_period = _PERIOD_STARS_RE.search(block)
        period_stars = _clean_text(m_period.group(1)) if m_period else ""

        prev_date = seen_history.get(url_key(repo_url))

        repos.append({
            "rank": idx,
            "repo": repo_name,
            "url": repo_url,
            "description": desc,
            "language": lang,
            "total_stars": total_stars,
            "forks": forks,
            "period_stars": period_stars,
            "prev_date": prev_date,  # 若之前已推送过，记录上次推送日期如 "11月08日"
            "readme_excerpt": "",
        })

    # 并发拉取 README 摘要
    if repos:
        with ThreadPoolExecutor(max_workers=5) as pool:
            futs = [(r, pool.submit(_fetch_repo_readme, r["repo"])) for r in repos]
            for r, fut in futs:
                try:
                    r["readme_excerpt"] = fut.result(timeout=12)
                except Exception:
                    r["readme_excerpt"] = ""

    return {
        "since": since,
        "url": url,
        "repos": repos,
        "raw_count": len(articles),
    }


def _fetch_hn_item_details(item: dict[str, Any]) -> None:
    """补充单条 HN 帖子的外链正文摘要 + 评论区高赞前 4 条核心观点。"""
    oid = item["id"]
    ext_url = item.get("url", "")

    # 1. 抓外链正文摘要（若是外部链接）
    if ext_url and not ext_url.startswith("https://news.ycombinator.com"):
        try:
            item["article_excerpt"] = fetch_article_text(ext_url, timeout=8, max_chars=1000, min_chars=120)
        except Exception:
            item["article_excerpt"] = ""

    # 2. 抓 Algolia items/{id} 的顶层评论与正文（Ask HN / Show HN）
    try:
        resp = requests.get(
            f"https://hn.algolia.com/api/v1/items/{oid}",
            headers={"User-Agent": _UA},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            if not item.get("article_excerpt") and data.get("text"):
                item["article_excerpt"] = _clean_text(data.get("text", ""))[:1000]
            children = data.get("children") or []
            comments: list[str] = []
            # Algolia 的顶层 children 默认按 HN 热度排列，取有实质内容的前 4 条
            for ch in children:
                c_text = _clean_text(ch.get("text") or "")
                author = ch.get("author") or "anon"
                if len(c_text) >= 50:
                    # 统计子回复数以辅助衡量讨论热度
                    sub_replies = len(ch.get("children") or [])
                    reply_tag = f"（{sub_replies} 条追评）" if sub_replies else ""
                    comments.append(f"@{author}{reply_tag}：{c_text[:360]}")
                if len(comments) >= 4:
                    break
            item["top_comments"] = comments
    except Exception:
        pass


def fetch_hn_top8(window_h: int = 36, limit: int = 8,
                  seen_history: dict[str, str] | None = None) -> dict[str, Any]:
    """融合 Hacker News 的 Best（高分精选榜）与 Top（实时首页高讨论热榜），取综合热度前 8 名。"""
    seen_history = seen_history or {}
    cutoff = int(time.time()) - window_h * 3600

    # 1. 先从 HN 官方 Firebase API 获取实时 Top 榜前 25 名与 Best 榜前 25 名的 ID 集合，标记榜单属性
    top_ids: set[str] = set()
    best_ids: set[str] = set()
    try:
        r_top = requests.get("https://hacker-news.firebaseio.com/v0/topstories.json", timeout=8)
        if r_top.status_code == 200:
            top_ids = {str(x) for x in (r_top.json() or [])[:25]}
    except Exception:
        pass
    try:
        r_best = requests.get("https://hacker-news.firebaseio.com/v0/beststories.json", timeout=8)
        if r_best.status_code == 200:
            best_ids = {str(x) for x in (r_best.json() or [])[:25]}
    except Exception:
        pass

    by_id: dict[str, dict[str, Any]] = {}

    urls = [
        f"https://hn.algolia.com/api/v1/search?tags=story&numericFilters=created_at_i>{cutoff}&hitsPerPage=30",
        "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=30",
    ]
    for api_url in urls:
        try:
            resp = requests.get(api_url, headers={"User-Agent": _UA}, timeout=15)
            resp.raise_for_status()
            hits = resp.json().get("hits") or []
            for h in hits:
                oid = str(h.get("objectID") or "")
                if not oid:
                    continue
                created_i = int(h.get("created_at_i") or 0)
                if created_i and created_i < int(time.time()) - 48 * 3600:
                    continue
                pts = int(h.get("points") or 0)
                n_comments = int(h.get("num_comments") or 0)
                if oid not in by_id or pts > by_id[oid]["points"]:
                    hn_url = f"https://news.ycombinator.com/item?id={oid}"
                    ext_url = h.get("url") or hn_url
                    prev_date = (
                        seen_history.get(url_key(hn_url))
                        or seen_history.get(url_key(ext_url))
                    )
                    # 标记来自 Best 还是 Top 实时热议
                    badges = []
                    if oid in best_ids:
                        badges.append("Best高赞")
                    if oid in top_ids:
                        badges.append("Top实时热议")
                    badge_str = "/".join(badges) if badges else "24h热榜"

                    # 综合热度分：兼顾 Points 与 评论讨论激烈度（让 Top 榜高讨论度新帖也能入选）
                    heat_score = pts + int(n_comments * 1.2) + (80 if oid in top_ids else 0) + (60 if oid in best_ids else 0)

                    by_id[oid] = {
                        "id": oid,
                        "title": (h.get("title") or "").strip(),
                        "url": ext_url,
                        "hn_url": hn_url,
                        "points": pts,
                        "num_comments": n_comments,
                        "badge": badge_str,
                        "heat_score": heat_score,
                        "author": h.get("author") or "",
                        "created_at": h.get("created_at") or "",
                        "prev_date": prev_date,
                        "article_excerpt": "",
                        "top_comments": [],
                    }
        except Exception as exc:
            print(f"[sources] HN API 请求异常 ({api_url}): {exc}", file=sys.stderr)

    sorted_items = sorted(by_id.values(), key=lambda x: x["heat_score"], reverse=True)
    top_items = sorted_items[:limit]

    if top_items:
        with ThreadPoolExecutor(max_workers=6) as pool:
            futs = [pool.submit(_fetch_hn_item_details, item) for item in top_items]
            for fut in futs:
                try:
                    fut.result(timeout=15)
                except Exception:
                    pass

    return {
        "items": top_items,
        "raw_count": len(by_id),
    }
