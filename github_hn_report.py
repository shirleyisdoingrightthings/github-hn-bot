#!/usr/bin/python3
"""
GitHub Trending & Hacker News 早报

每天早 09:30 自动抓取 GitHub Trending 与 Hacker News 热榜前八，
由 AI 定时任务写稿后推送到飞书。
- 周一至周六（或 --since daily）：抓取 GitHub Daily 飙升榜（since=daily）+ Hacker News 24h 热榜 Top 8，输出 === FETCH_OK ===
- 周日（或 --since weekly）：抓取 GitHub Weekly 周趋势榜（since=weekly）+ Hacker News 24h 热榜 Top 8，输出 === WEEKLY_OK ===

跨天重复处理规则：
- 本地维护 logs/seen_history.json（记录过去 30 天已推送过的仓库与 HN 帖子及其首次推送日期 MM月DD日）。
- 若某个仓库或 HN 热帖在此前已推送过，fetch 阶段会在 context 中标注「🔁 重复在榜（首次推送于 MM月DD日）」，
  由写稿模型简述该项目/新闻并注明「（见 MM月DD日 推送）」。
- 周榜（since=weekly）额外支持「🔥 持续霸榜」汇总模块。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
from bot_utils import (
    already_ran_today,
    archive_and_sync_notion,
    emit_fetch_output,
    extract_hrefs,
    extract_report_date,
    make_logger,
    make_pending_saver,
    proxy_ok,
    sanitize_html,
    send_feishu,
)

import sources
from sources import url_key  # HN 讨论串保留 ?id= 的去重键（见 sources.url_key）

BASE = Path(__file__).parent
LOG_FILE = BASE / "logs" / "run.log"
JSONL_FILE = BASE / "logs" / "run.jsonl"
LOG_FILE.parent.mkdir(exist_ok=True)
ARCHIVE_DIR = BASE / "logs" / "archive"
ARCHIVE_DIR.mkdir(exist_ok=True)

CACHE_FILE = BASE / "pending_messages.json"
DRAFT_FILE = BASE / "logs" / "report_draft.txt"
FETCH_META = BASE / "logs" / "fetch_meta.json"
LAST_CONTEXT = BASE / "logs" / "last_context.txt"
SEEN_HISTORY = BASE / "logs" / "seen_history.json"
HISTORY_KEEP_DAYS = 30

FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK", "")

write_log = make_logger(LOG_FILE, JSONL_FILE)
save_pending = make_pending_saver(CACHE_FILE)
_PROXY = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
os.environ.setdefault("HTTP_PROXY", _PROXY or "")
os.environ.setdefault("HTTPS_PROXY", _PROXY or "")


def _proxy_ok() -> bool:
    global _PROXY
    ok, _PROXY = proxy_ok(_PROXY, None)
    return ok


def load_seen_history(path: Path) -> dict[str, str]:
    """读取历史已推送记录，返回 {url_key: 'MM月DD日'}（仅含今天之前的记录）。"""
    today_iso = datetime.now().strftime("%Y-%m-%d")
    cutoff_iso = (datetime.now() - timedelta(days=HISTORY_KEEP_DAYS)).strftime("%Y-%m-%d")
    result: dict[str, str] = {}
    if not path.exists():
        return result
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return result
        for k, v in data.items():
            if isinstance(v, dict):
                iso_d = v.get("iso_date", "")
                disp_d = v.get("display_date", "")
                if iso_d and iso_d >= cutoff_iso and iso_d < today_iso and disp_d:
                    result[k] = disp_d
    except Exception:
        pass
    return result


def record_seen_history(path: Path, urls: list[str]) -> int:
    """将本次成功推送的 URL 记录到 seen_history.json。若此前已有记录，保留最早那次的推送日期。"""
    now = datetime.now()
    today_iso = now.strftime("%Y-%m-%d")
    today_disp = f"{now.month}月{now.day}日"
    cutoff_iso = (now - timedelta(days=HISTORY_KEEP_DAYS)).strftime("%Y-%m-%d")

    data: dict[str, dict[str, str]] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if isinstance(v, dict) and v.get("iso_date", "") >= cutoff_iso:
                        data[k] = v
        except Exception:
            data = {}

    for u in urls:
        k = url_key(u)
        if not k:
            continue
        if k not in data:
            data[k] = {
                "iso_date": today_iso,
                "display_date": today_disp,
                "last_iso_date": today_iso,
            }
        else:
            data[k]["last_iso_date"] = today_iso

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(data)


def run_fetch(since_override: str | None = None) -> int:
    if already_ran_today(LOG_FILE):
        emit_fetch_output(["=== SKIP_ALREADY_RAN ==="], LAST_CONTEXT)
        return 0

    if not _proxy_ok():
        write_log("WARN", f"代理不可用（{_PROXY}），跳过抓取")
        emit_fetch_output(["=== SKIP_PROXY ==="], LAST_CONTEXT)
        return 0

    now = datetime.now()
    today_str = f"{now.year}年{now.month}月{now.day}日"
    is_sunday = (now.weekday() == 6)

    since = since_override or os.getenv("GH_TRENDING_SINCE", "").strip().lower()
    if since not in ("daily", "weekly"):
        since = "weekly" if is_sunday else "daily"

    seen = load_seen_history(SEEN_HISTORY)
    gh_limit = 10 if since == "daily" else 12

    print(f"📡 正在抓取 GitHub Trending (since={since}, Top {gh_limit}) 与 Hacker News (Best+Top) Top 8...", file=sys.stderr)

    try:
        gh_data = sources.fetch_github_trending(since=since, limit=gh_limit, seen_history=seen)
    except Exception as exc:
        gh_data = {"since": since, "url": f"https://github.com/trending?since={since}", "repos": [], "raw_count": 0}
        print(f"  ⚠️ GitHub Trending 抓取异常：{exc}", file=sys.stderr)

    try:
        hn_data = sources.fetch_hn_top8(window_h=36, limit=8, seen_history=seen)
    except Exception as exc:
        hn_data = {"items": [], "raw_count": 0}
        print(f"  ⚠️ Hacker News 抓取异常：{exc}", file=sys.stderr)

    repos = gh_data["repos"]
    hn_items = hn_data["items"]

    if not repos and not hn_items:
        write_log("WARN", "GitHub Trending 与 Hacker News 均未抓到有效条目")
        emit_fetch_output(["=== NO_DATA ==="], LAST_CONTEXT)
        return 0

    marker = "=== WEEKLY_OK ===" if since == "weekly" else "=== FETCH_OK ==="
    out: list[str] = [
        marker,
        "=== CONTEXT_BEGIN ===",
        f"今天日期：{today_str}",
        f"榜单模式：{'GitHub Weekly 本周趋势榜 + Hacker News (Best+Top) 热榜 Top 8' if since == 'weekly' else 'GitHub Daily 今日飙升榜 + Hacker News (Best+Top) 热榜 Top 8'}",
        f"GitHub 源地址：{gh_data['url']}",
        "",
        "==========================================",
        f"【第一部分：GitHub Trending ({since.upper()}) Top {len(repos)}】",
        "==========================================",
    ]

    repeat_repos = 0
    for r in repos:
        status_str = (
            f"🔁 重复霸榜（首次见于 {r['prev_date']} 推送）"
            if r["prev_date"]
            else "🆕 首次上榜"
        )
        if r["prev_date"]:
            repeat_repos += 1
        out.append(f"\n[GitHub #{r['rank']}] {r['repo']}")
        out.append(f"- 链接：{r['url']}")
        out.append(f"- 状态：{status_str}")
        out.append(f"- 语言：{r['language']} | 总 Star：{r['total_stars']} | Fork：{r['forks']} | 周期增量：{r['period_stars']}")
        out.append(f"- 官方简介：{r['description'] or '（无简介）'}")
        if r["readme_excerpt"]:
            out.append(f"- README 详情摘录：{r['readme_excerpt']}")

    out.extend([
        "",
        "==========================================",
        f"【第二部分：Hacker News (Best高赞 + Top实时热议) Top {len(hn_items)}】",
        "==========================================",
    ])

    repeat_hn = 0
    for idx, item in enumerate(hn_items, 1):
        status_str = (
            f"🔁 重复在榜（首次见于 {item['prev_date']} 推送）"
            if item["prev_date"]
            else "🆕 首次上榜"
        )
        if item["prev_date"]:
            repeat_hn += 1
        out.append(f"\n[HN #{idx}] {item['title']}")
        out.append(f"- 原文链接：{item['url']}")
        out.append(f"- HN 讨论串：{item['hn_url']}")
        out.append(f"- 榜单来源：{item.get('badge', '热榜')}")
        out.append(f"- 状态：{status_str}")
        out.append(f"- 热度：{item['points']} points | {item['num_comments']} 条评论 | 发布者：{item['author']}")
        if item["article_excerpt"]:
            out.append(f"- 原文内容摘要：{item['article_excerpt']}")
        if item["top_comments"]:
            out.append("- 评论区高赞观点：")
            for c in item["top_comments"]:
                out.append(f"  * {c}")

    out.append("=== CONTEXT_END ===")

    track_urls = [r["url"] for r in repos] + [it["hn_url"] for it in hn_items] + [it["url"] for it in hn_items]
    meta = {
        "date_str": now.strftime("%Y-%m-%d"),
        "log_summary": f"GitHub({since}) {len(repos)}个(含重{repeat_repos}) + HN {len(hn_items)}条(含重{repeat_hn})",
        "metrics": {
            "since": since,
            "gh_count": len(repos),
            "gh_repeat": repeat_repos,
            "hn_count": len(hn_items),
            "hn_repeat": repeat_hn,
            "track_urls": track_urls,
        },
    }
    FETCH_META.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    emit_fetch_output(out, LAST_CONTEXT)
    return 0


def run_send(since_override: str | None = None) -> int:
    t0 = time.time()
    if already_ran_today(LOG_FILE):
        print("今天已成功运行过，跳过发送。如需强制请设置 FORCE_RUN=1。", file=sys.stderr)
        return 0

    if not DRAFT_FILE.exists():
        write_log("FAIL", f"稿子文件不存在：{DRAFT_FILE}")
        return 1

    report = DRAFT_FILE.read_text(encoding="utf-8").strip()
    if not report:
        write_log("FAIL", f"稿子文件为空：{DRAFT_FILE}")
        return 1

    report = sanitize_html(report)
    hrefs = extract_hrefs(report)
    if not hrefs:
        write_log("FAIL", "稿件里没有任何 <a href> 链接，拒绝发送")
        return 1

    save_pending([report])
    print("📨 推送到飞书...", file=sys.stderr)
    sent = send_feishu(report, FEISHU_WEBHOOK)
    print(f"  ✓ 推送成功（{sent} 条）", file=sys.stderr)
    CACHE_FILE.unlink(missing_ok=True)

    try:
        meta = json.loads(FETCH_META.read_text(encoding="utf-8"))
    except Exception:
        meta = {"log_summary": "", "metrics": {}}

    since = (
        since_override
        or os.getenv("GH_TRENDING_SINCE", "").strip().lower()
        or str(meta.get("metrics", {}).get("since", "")).strip().lower()
    )
    if since not in ("daily", "weekly"):
        since = "weekly" if datetime.now().weekday() == 6 else "daily"
    is_weekly = (since == "weekly")

    # 归档当日成稿并同步到 Notion「每日情报归档与决策看板」数据库
    doc_date = extract_report_date(report) or meta.get("date_str") or datetime.now().strftime("%Y-%m-%d")
    notion_meta = archive_and_sync_notion(
        report,
        "🐙 GitHub & HN",
        date_str=doc_date,
        archive_dir=ARCHIVE_DIR,
        archive_filename=f"{doc_date}.txt",
        report_type="周盘点" if is_weekly else "日报",
    )
    notion_tag = " | Notion ✓" if notion_meta.get("notion_ok") else " | Notion ⚠️"

    track_urls = meta.get("metrics", {}).get("track_urls", []) or []
    total_seen = record_seen_history(SEEN_HISTORY, list(hrefs) + list(track_urls))
    print(f"  ✓ 已更新跨天在榜记忆库（共记录 {total_seen} 条 URL）", file=sys.stderr)

    duration = round(time.time() - t0, 1)
    metrics = {k: v for k, v in meta.get("metrics", {}).items() if k != "track_urls"}
    write_log(
        "OK",
        f"AI写稿 → {meta.get('log_summary', '')} → 飞书推送成功（{sent} 条 / {len(report)}字）{notion_tag}",
        metrics={**metrics, **notion_meta, "duration_s": duration, "report_chars": len(report)},
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="GitHub Trending & Hacker News 早报")
    parser.add_argument("--mode", choices=["fetch", "send"], required=True)
    parser.add_argument("--since", choices=["daily", "weekly"], default=None,
                        help="强制指定 GitHub Trending 时间跨度（默认周一至周六 daily，周日 weekly）")
    args = parser.parse_args()
    if args.mode == "fetch":
        return run_fetch(since_override=args.since)
    return run_send(since_override=args.since)


if __name__ == "__main__":
    sys.exit(main())
