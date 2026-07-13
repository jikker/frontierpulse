#!/usr/bin/env bash
# FrontierPulse cron runner（預設驅動已登入的 Grok Web，免 Build/API 額度）
# crontab 範例見 crontab.example
set -u
cd "$(dirname "$0")"
export DIGEST_BACKEND="${DIGEST_BACKEND:-webgrok}"
export WEBGROK_GEN_TIMEOUT="${WEBGROK_GEN_TIMEOUT:-300}"
# 公開 JSON 加密（選用；設定後 App 端 Config.swift 要內嵌同一把金鑰）：
# export FRONTIERPULSE_ENC_KEY="<64 hex>"
SCRIPT=ai_digest.py
if [ "$DIGEST_BACKEND" = "webgrok" ]; then
  SCRIPT=ai_digest_webgrok.py
fi
python3 "$SCRIPT" >> digest.log 2>&1
