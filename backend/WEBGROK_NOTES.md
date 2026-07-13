# FrontierPulse WebGrok 後端

`ai_digest_webgrok.py` 依照 MarsRadar 已驗證的方式，透過 browser MCP 操作已登入的
`grok.com`，要求 Grok 即時讀取官方網站與 X，再把結構化 JSON 交回既有
`ai_digest.py` 做合併、模型追蹤、index/latest 重建與 git push。

## 執行條件

- browser MCP daemon：預設 `http://127.0.0.1:3457`
- Chrome 已開啟且 Grok Web 已登入
- 同一 Grok 分頁不可同時被其他自動化占用

## 用法

```bash
# 正式跑並 push（run.sh 預設就是 webgrok）
./run.sh

# 只驗證、不 push
NO_PUSH=1 python3 ai_digest_webgrok.py
```

環境變數：

- `FRONTIERPULSE_MCP_BASE`：browser MCP base URL
- `WEBGROK_LOOKBACK_HOURS`：回看時數，預設 12
- `WEBGROK_GEN_TIMEOUT`：等待 Grok 回覆秒數，預設 300
- `WEBGROK_REUSE_TAB=0`：不複用既有 Grok 對話分頁
- `NO_PUSH=1`：不推送 GitHub

cron 仍執行 `run.sh`，不需要改排程。若瀏覽器或擴充功能未連線，該輪會清楚失敗並在
`digest.log` 留下原因，下輪 cron 自動重試。
