#!/bin/bash
# AgentForge → GitHub 自动同步（cron 每 5 分钟）
# token 存于 /root/.gh_token（不入库），有变更才提交推送
cd /opt/agentforge || exit 1
export GIT_TERMINAL_PROMPT=0
TOKEN=$(cat /root/.gh_token 2>/dev/null)
[ -z "$TOKEN" ] && exit 1

if ! git remote get-url origin >/dev/null 2>&1; then
  git remote add origin https://x-access-token:${TOKEN}@github.com/wamgfemg/agent-demo.git
fi

if [ -z "$(git status --porcelain)" ]; then
  exit 0
fi

TS=$(date "+%Y-%m-%d %H:%M")
git add -A
git -c user.name="wamgfemg" -c user.email="505813@163.com"   commit -m "自动同步：服务器代码更新 ${TS}" --quiet
git push origin main --quiet 2>/dev/null && echo "synced ${TS}" >> /opt/agentforge/logs/sync.log
