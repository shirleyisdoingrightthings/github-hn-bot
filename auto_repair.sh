#!/bin/bash
# auto_repair.sh — GitHub Trending & Hacker News 早报
# 检测到 [FAIL] 时由 health_check.sh 触发（委托 ../shared/auto_repair_base.sh）

DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_NAME="GitHub Trending & HN Morning Bot"
SCRIPT="$DIR/github_hn_report.py"
ERROR="${1:-unknown error}"
DRAFTS="logs/report_draft.txt"

source "$DIR/../shared/auto_repair_base.sh"
