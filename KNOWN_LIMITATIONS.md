# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目尚未滿足,已留待後續改良:

- [ ] 釐正 line_webhook.py:409 的誤導性 NOTE 註解，改述「reply 阻塞 I/O 在 BackgroundTasks 下已被 run_in_threadpool 移出 event loop，asyncio.to_thread 為冗餘錯誤方向」
