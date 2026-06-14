# 已知限制（Known Limitations）

本次以「核心可用、帶已知限制」版本交付；以下項目尚未滿足,已留待後續改良:

- [ ] 在 routers/tenants.py 新增 GET/PUT/DELETE /tenants/me/line-config 三端點，tenant_id 取自 current_user.tenant_id，直接呼叫既有 line_config service（不改 service 層）
