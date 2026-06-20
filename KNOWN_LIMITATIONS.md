# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目尚未滿足,已留待後續改良:

- [ ] `base.py` 含 `TranslationResult`，為 `@dataclass(frozen=True)`，欄位含 `text: str`、`detected_lang: str | None`、`skipped: bool`；`Translator.translate` 型別標註回傳 `TranslationResult`。
