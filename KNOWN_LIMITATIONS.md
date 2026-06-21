# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目尚未滿足,已留待後續改良:

- [ ] `base.py` 含 `TranslationResult`，為 `@dataclass(frozen=True)`，欄位含 `text: str`、`detected_lang: str | None`、`skipped: bool`；`Translator.translate` 型別標註回傳 `TranslationResult`。
- [ ] 管理 UI（`/ui/*`）的 CSRF 防護：目前僅依賴 `SameSite=Lax` cookie + 同源伺服，未實作 per-request CSRF token。對同源、無子網域的 MVP 後台可接受；若日後將 UI 暴露給不可信的同源來源，應加上 double-submit cookie token。
