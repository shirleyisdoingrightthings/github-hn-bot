#!/bin/bash
# health_check.sh — GitHub Trending & Hacker News 早报
# 每天 10:30 检查当天是否已成功推送（Option B 轻量化健康检查）

DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_NAME="GitHub Trending & HN Morning Bot"

# 周末/工作日早报 09:30 运行，10:30 体检落在窗口内（>= 10）
CATCHUP_FROM=10

NO_NEWS_PATTERN="均未抓到有效条目"
NO_NEWS_MSG="今天未抓到有效条目，未出稿"

STALE_KEY="stale_sources"
ZERO_KEY="zero_sources"

content_check() {
    local gh_cnt
    gh_cnt=$(jsonl_field gh_count 99)
    if [ -n "$gh_cnt" ] && [ "$gh_cnt" -lt 3 ] 2>/dev/null; then
        notify WARN "本次 GitHub Trending 仅抓到 ${gh_cnt} 个项目，请检查页面结构"
        echo "[health_check] WARN: gh_count=${gh_cnt}，数量偏低"
    fi
}

source "$DIR/../shared/health_check_base.sh"
