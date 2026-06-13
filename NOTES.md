## 任務 #1 完成：設計並建立 src layout 套件結構與 pyproject.toml，含 entry point 與 `__main__.py` 雙入口、釘選可 pip 安裝的相依

## 任務 #2 完成：實作帳號模組：註冊/登入、密碼雜湊儲存、簽發可驗證的 session/token

## 任務 #3 完成：實作多租戶資料模型與隔離層，使用者隸屬租戶、跨租戶存取一律拒絕

## 異議退回 任務 #4：實作 plan/quota 邏輯（free/pro），計量 API 用量，超量回明確錯誤
先看專案結構與 quota 相關實作。
已查核 task #4（quota）的實作、測試與接線。task #4 自身的 quota 單元測試全綠，free/pro 配額明確、429 訊息明確、`isinstance(bool)` 排除也有。但有實質問題：

**1.（主要）計量點只覆蓋「建立」，名不副實的「計量 API 用量」**
`require_quota` 只接在 `POST /notes/` 一個端點。`GET /notes/`、`GET /notes/{id}`、`PUT`、`DELETE` 全部不計量、不受配額管控。結果：
- 任務描述「計量 API 用量」實際只計到「建立筆記」，讀/改/刪完全不進計數器。
- free 用戶可無限次 GET/PUT/DELETE，繞過配額。
- 直接牴觸驗收標準 #6「所有操作受認證＋租戶＋quota 管控」。
這是 task #4 核心交付（計量點設計）的實質缺口，不是純 task #5 接線問題。

**2.（次要）日界用 local time，與模型註解的 UTC 不符**
`quota.py` 用 `datetime.date.today()`（本地時區），但 `models/usage.py` 註解寫「UTC date」。配額重置時點不一致，屬定義模糊／文件不符。

**3.（須提醒，非 task #4 之過）整體 `pytest` 未全綠**
`python -m pytest -q` 有 1 紅：`test_task1_structure.py::test_all_deps_importable` 仍斷言 `import jose`，但專案已改用 PyJWT（pyproject 無 python-jose）。這是 task #1 的過時測試，根因不在 task #4，但執行指令 `python -m pytest -q && python -m saas_mvp` 會在 pytest 步驟失敗，違反驗收標準 #7「pytest 全綠」。需有人修這條 stale 測試，整體才能驗收。

第 1 點足以退回 task #4：計量未覆蓋讀/改/刪，配額無法真正管控 API 用量。

異議: 成立

## 任務 #4 完成：實作 plan/quota 邏輯（free/pro），計量 API 用量，超量回明確錯誤

## 任務 #5 完成：實作核心資源 CRUD REST API（如 notes），受帳號＋租戶＋quota 管控，路由與狀態碼清楚

