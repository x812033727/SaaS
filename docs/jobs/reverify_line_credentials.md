# LINE 憑證背景重驗

`python -m saas_mvp.ops.reverify_line_credentials` 會掃描上次驗證超過
24 小時、且目前狀態為 `valid` 的 LINE Bot 設定。部署的
supercronic 每 6 小時執行一次，單次預設最多 100 筆。

## 安全限制

- 每個租戶每小時最多呼叫 bot/info 3 次，手動與背景驗證共用預算。
- 每筆預設間隔 5 秒，避免在 LINE API 產生尖峰。
- 連續 10 筆系統／網路錯誤時開啟 circuit breaker，當次以 exit 1 結束並寫 log。
- `invalid` / `conflict` 不自動重試，等租戶修正憑證，避免無效流量。

## 設定

| 環境變數 | 預設 | 說明 |
|---|---:|---|
| `SAAS_LINE_VERIFY_MAX_ATTEMPTS_PER_HOUR` | 3 | 單租戶每小時驗證預算 |
| `SAAS_LINE_CREDENTIAL_REVERIFY_HOURS` | 24 | 多久後視為 stale |
| `SAAS_LINE_CREDENTIAL_REVERIFY_BATCH_SIZE` | 100 | 單次最大掃描量 |

手動巡檢可用 `--threshold-hours`、`--batch-size` 與
`--throttle-seconds` 覆寫當次參數。輸出只含統計，不會列出 token、secret
或其他敏感值。
