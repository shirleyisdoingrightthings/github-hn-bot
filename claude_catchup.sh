#!/bin/bash
# claude_catchup.sh — GitHub Trending & Hacker News 早报
# 当天 09:30 未成功出稿时的无头补跑（开机唤醒后由 health_check.sh 触发）

DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_NAME="GitHub Trending & HN Morning Bot"
PLIST="$HOME/Library/LaunchAgents/com.shirley.github-hn-bot.plist"
WRITE_SPEC="严格按 prompt.md 写稿并写入 logs/report_draft.txt（若 fetch 输出的是 === WEEKLY_OK === 而非 === FETCH_OK ===，说明今天是周日，改按 prompt_weekly.md 写周榜稿，写入同一个 logs/report_draft.txt）"

source "$DIR/../shared/headless_catchup_base.sh"
