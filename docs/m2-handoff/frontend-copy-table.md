# LINE 憑證狀態前端文案對照表

> Living document：後端只回傳狀態碼與安全的錯誤分類，不回傳本表文案。
> 前端可依產品語調微調，但不應改變操作指引的語意。

## 狀態

| code | zh-TW | English |
|---|---|---|
| `unchecked` | 尚未檢查 LINE 連線，請執行連線測試。 | LINE connection has not been checked. Run a connection test. |
| `valid` | LINE 憑證有效，Bot 可正常連線。 | LINE credentials are valid and the bot is connected. |
| `invalid` | LINE 拒絕憑證，請依下方指引核對設定。 | LINE rejected the credentials. Review the guidance below. |
| `error` | 暫時無法連線 LINE；請稍後重試，不需先刪除憑證。 | LINE is temporarily unavailable. Retry later; do not delete credentials yet. |
| `conflict` | 這個 LINE Bot 已連結其他店家，請確認 Provider 與帳號歸屬。 | This LINE bot is connected to another shop. Verify provider and ownership. |

## 401 分類

| error kind | zh-TW | English |
|---|---|---|
| `ACCESS_TOKEN_INVALID` | Channel access token 錯誤或已失效；請從 LINE Developers 重新複製完整 token。 | The channel access token is invalid or expired. Copy a fresh full token from LINE Developers. |
| `CHANNEL_SECRET_INVALID` | Channel secret 不正確；請核對 Messaging API channel，並移除多餘空白。 | The channel secret is invalid. Check the Messaging API channel and remove extra whitespace. |
| `UNKNOWN_AUTH` | LINE 無法辨識憑證；請同時核對 access token 與 channel secret。 | LINE could not authenticate the channel. Check both access token and channel secret. |

## 驗證預算

| error | zh-TW | English |
|---|---|---|
| `rate_limited: retry after {minutes}m` | 連線測試次數過多，請在 `{minutes}` 分鐘後重試。 | Too many connection tests. Retry in `{minutes}` minutes. |
