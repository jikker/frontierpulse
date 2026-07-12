#!/usr/bin/env bash
# FrontierPulse cron runner（本機 Grok CLI 後端，免 API key）
# crontab 範例見 crontab.example
set -u
cd "$(dirname "$0")"
export DIGEST_BACKEND="${DIGEST_BACKEND:-cli}"
export GROK_TIMEOUT="${GROK_TIMEOUT:-1800}"
# grok 不在 cron PATH 時取消下行註解：
# export GROK_BIN="$HOME/.grok/bin/grok"
# 公開 JSON 加密（選用；設定後 App 端 Config.swift 要內嵌同一把金鑰）：
# export FRONTIERPULSE_ENC_KEY="<64 hex>"
python3 ai_digest.py >> digest.log 2>&1
