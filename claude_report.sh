#!/bin/bash
# claude_report.sh — 供定时任务调用的 fetch/send 封装
#
# 用法：
#   bash claude_report.sh fetch   # 取材并把 context 打到 stdout（同时落盘 logs/last_context.txt）
#   bash claude_report.sh send    # 读取 logs/report_draft.txt 并推送飞书

set -uo pipefail
cd "$(dirname "$0")" || exit 1

MODE="${1:-}"
if [ "$MODE" != "fetch" ] && [ "$MODE" != "send" ]; then
    echo "ERROR: 用法 claude_report.sh fetch|send" >&2
    exit 2
fi

PLIST="$HOME/Library/LaunchAgents/com.shirley.github-hn-bot.plist"
PY="/usr/bin/python3"

if [ ! -f "$PLIST" ]; then
    echo "ERROR: 找不到 $PLIST，无法加载环境变量" >&2
    exit 1
fi

while IFS= read -r line; do
    export "$line"
done < <(/usr/libexec/PlistBuddy -c "Print :EnvironmentVariables" "$PLIST" \
    | sed -n 's/^[[:space:]]*\([A-Za-z_][A-Za-z0-9_]*\) = \(.*\)$/\1=\2/p')

exec "$PY" github_hn_report.py --mode "$MODE"
