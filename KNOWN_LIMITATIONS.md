# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目尚未滿足,已留待後續改良:

- [ ] 新增 /usage 端點：回傳目前租戶總量、per-key 明細與各自剩餘 quota

## Task #3 — 速率限制（新增）

- **Retry-After 精度保守**：超限時 `Retry-After` header 回傳完整視窗長度（如 60），
  而非「最舊請求到期時間 - 現在」的精確剩餘秒數。RFC 6585 允許固定值，
  對 client 不友善但合規。精確做法：
  ```python
  oldest = calls[0]
  retry_after = int(oldest + self._window - now) + 1
  headers={"Retry-After": str(max(1, retry_after))}
  ```

- **其他業務端點未掛 `require_rate_limit`**：本次僅 `/notes/*` 加上速率限制；
  `/quota/status`、`/usage/`、`/api-keys/` 尚未覆蓋。
  刻意排除原因：這三類端點流量遠低於 notes CRUD，且受認證保護；
  待後續依實際監控數據補齊。

- **單 worker 限定**：`SlidingWindowRateLimiter` 使用 in-memory `_BoundedTimestampLog`，
  不跨 process 共享。多 worker 部署需改用 Redis 後端。
