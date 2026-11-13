#!/bin/bash
# claude_report.sh — 供定时任务调用的薄封装（委托 shared/report_base.sh）
DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.shirley.github-hn-bot.plist"
SCRIPT="github_hn_report.py"
USAGE_MSG="用法: claude_report.sh {fetch|send} [daily|weekly]"
EXTRA_ARGS=()
if [ "${1:-}" = "fetch" ] && { [ "${2:-}" = "daily" ] || [ "${2:-}" = "weekly" ]; }; then
    EXTRA_ARGS=(--since "$2")
fi
source "$DIR/../shared/report_base.sh"
