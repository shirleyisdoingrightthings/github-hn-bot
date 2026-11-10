#!/bin/bash
# health_check.sh — GitHub Trending & Hacker News 早报
# 每天 10:30 及开机唤醒时检查今天是否已成功推送，若未推送则自动触发 claude_catchup.sh 无头补跑

DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_NAME="GitHub Trending & HN Morning Bot"

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
