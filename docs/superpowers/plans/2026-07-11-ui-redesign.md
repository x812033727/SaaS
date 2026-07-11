# UI 全面重設計(溫暖精品風)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 LINE 綠工具風後台升級為「溫暖精品風」設計系統:側邊欄分組導覽 + design tokens + 對外頁門面化,4 支獨立 PR。

**Architecture:** 換皮不換骨——沿用既有 class 名(`.card` `.btn` `.badge` `.stat` `.grid` `table`)重寫 `app.css`,70+ 模板零改動;只重寫 `base.html`(側邊欄)與少數頁面結構(dashboard、booking_form、public、auth)。

**Tech Stack:** Jinja2 + htmx + 單檔 CSS(無 build step、無新 JS)。

## Global Constraints

- 使用者可見文案**一字不動**(測試斷言「已儲存」「尚未開通」「預約成功」「請選擇時段」「開始預約」等中文字串)。
- 表單 `name`/`action`/`method`、`hx-*` 屬性、元素 `id` 一律不動。
- 互動維持 CSS-only(checkbox / `<details>`),不新增 `<script>`。
- 手機表格塌縮機制(`data-label`)保留。
- 開發僅在 `/root/saas-dev`(branch 基於 origin/main=f0f83c9),絕不碰 `/opt/saas`。
- 每支 PR 收尾必跑 `bash run_tests.sh` 全綠(約 4 分鐘,基準 2100 passed;本機 9 個 pip 環境性失敗與 main 相同可忽略)。
- PR 用 `gh pr create`,**不合併**(合併=production 部署,需使用者授權);stacked 時勿用 `--delete-branch`。
- 公開店家頁保留每租戶 `profile.theme_color` 的 `--theme` 變數機制。

## Design Tokens(所有 Task 共用,定義一次)

```css
:root {
  --bg: #FAF9F6;          /* 暖白底 */
  --card: #FFFFFF;
  --ink: #2B2B28;         /* 暖黑文字 */
  --muted: #8A867C;       /* 暖灰次要文字 */
  --line: #E9E5DD;        /* 暖色邊框 */
  --brand: #1A5C4A;       /* 深墨綠 */
  --brand-deep: #124436;
  --brand-soft: #EDF3F0;  /* 墨綠淡底 */
  --gold: #C4956A;        /* 琥珀金點綴 */
  --gold-soft: #F6EEE3;
  --danger: #B3564D;      /* 暖紅 */
  --danger-soft: #F9ECEA;
  --ok: #3E7D5F;
  --ok-soft: #E9F2ED;
  --warn: #B7791F;
  --warn-soft: #FBF3E4;
  --radius: 12px;
  --radius-sm: 8px;
  --shadow-1: 0 1px 2px rgba(43,43,40,.05), 0 2px 8px rgba(43,43,40,.04);
  --shadow-2: 0 4px 16px rgba(43,43,40,.10);
  --font: "Noto Sans TC", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
```

相容別名:舊 CSS 用到 `--text`、`--border`、`--accent`、`--accent-dark`(inline style 也可能引用)→ 在 `:root` 一併宣告映射:`--text: var(--ink); --border: var(--line); --accent: var(--brand); --accent-dark: var(--brand-deep);`

---

### Task 1(PR1):Design system CSS + base.html 側邊欄

**Files:**
- Modify: `src/saas_mvp/static/app.css`(整檔重寫,~500 行)
- Modify: `src/saas_mvp/templates/base.html`(導覽區重寫,main/AI widget 保留)

**Interfaces:**
- Produces: 全站 class 樣式(`.card` `.btn` `.btn.secondary` `.btn.danger` `.badge.{valid,invalid,error,conflict,unchecked,none,on,off}` `.stat` `.grid` `.row` `.col` `.muted` `.error` `.success` `.auth-wrap` `.center-link` 以及表格/表單/RWD 規則)——後續 Task 直接使用,不得改名。
- Produces: `base.html` 版面骨架:`.layout`(grid: 240px sidebar + 1fr main)、`.sidebar`、`.side-group`(details)、`.side-link`、`.topbar`。

- [ ] **Step 1:重寫 `base.html` 導覽結構**

保留:`<head>`(加 Google Fonts 免載——用系統 Noto Sans TC fallback,不加外部請求)、csrf hx-headers、代管橫幅(移到 `.layout` 之上)、`{% block content %}`、AI widget(class 不動,配色由 CSS 接手)。

新結構(連結文字與 href 一字不動,自 base.html:39-74 搬移):

```html
<body{% if csrf_token %} hx-headers='{"X-CSRF-Token": "{{ csrf_token }}"}'{% endif %}>
  {% if impersonating %}(原代管橫幅,原樣){% endif %}
  {% if current_user %}
  {% set path = request.url.path %}
  <input type="checkbox" id="nav-toggle" class="nav-toggle" aria-hidden="true">
  <div class="layout">
    <aside class="sidebar">
      <div class="side-brand">LINE Bot 管理</div>
      <nav class="side-nav">
        <a class="side-link{% if path == '/ui/' %} active{% endif %}" href="/ui/">儀表板</a>
        <details class="side-group" {% if path.startswith(('/ui/booking','/ui/calendar','/ui/customers','/ui/notes')) %}open{% endif %}>
          <summary>預約營運</summary>
          <a class="side-link{% if path.startswith('/ui/booking') %} active{% endif %}" href="/ui/booking">預約管理</a>
          <a ... href="/ui/calendar">行事曆</a>
          <a ... href="/ui/customers">顧客</a>
          <a ... href="/ui/notes">備註</a>
        </details>
        <details class="side-group" {% if path.startswith(('/ui/line-config','/ui/line-chat','/ui/auto-reply','/ui/rich-menu','/ui/flex-menu','/ui/faq')) %}open{% endif %}>
          <summary>LINE</summary>
          <a href="/ui/line-config">LINE 設定</a>
          <a href="/ui/line-chat">客服訊息</a>
          <a href="/ui/auto-reply">自動回覆</a>
          <a href="/ui/rich-menu">圖文選單</a>
          <a href="/ui/flex-menu">圖文卡片</a>
          <a href="/ui/faq">AI 客服</a>
        </details>
        <details class="side-group" {% if path.startswith(('/ui/services','/ui/pos','/ui/shop','/ui/coupons','/ui/locations','/ui/staff')) %}open{% endif %}>
          <summary>商店銷售</summary>
          <a href="/ui/services">服務項目</a>
          <a href="/ui/pos">POS 結帳</a>
          <a href="/ui/shop">商品</a>
          <a href="/ui/coupons">優惠券</a>
          <a href="/ui/locations">分店</a>
          <a href="/ui/staff">員工</a>
        </details>
        <details class="side-group" {% if path.startswith(('/ui/campaigns','/ui/notifications','/ui/portfolio','/ui/profile')) %}open{% endif %}>
          <summary>行銷</summary>
          <a href="/ui/campaigns">行銷活動</a>
          <a href="/ui/notifications">通知歷程</a>
          <a href="/ui/portfolio">作品集</a>
          <a href="/ui/profile">店家頁</a>
        </details>
        <a class="side-link{% if path.startswith('/ui/reports') %} active{% endif %}" href="/ui/reports">報表</a>
        <details class="side-group" {% if path.startswith(('/ui/plan','/ui/billing','/ui/features','/ui/api-keys','/ui/members','/ui/account')) %}open{% endif %}>
          <summary>帳務設定</summary>
          <a href="/ui/plan">方案</a>
          <a href="/ui/billing">帳單</a>
          <a href="/ui/features">進階功能</a>
          <a href="/ui/api-keys">API 金鑰</a>
          <a href="/ui/members">成員</a>
          <a href="/ui/account">帳號</a>
        </details>
        {% if is_admin %}
        <details class="side-group" {% if path.startswith('/ui/admin') %}open{% endif %}>
          <summary>平台管理</summary>
          <a href="/ui/admin">平台總覽</a>
          <a href="/ui/admin/bots">跨店總覽</a>
          <a href="/ui/admin/audit">稽核</a>
        </details>
        {% endif %}
      </nav>
      <div class="side-user">
        <span class="who">{{ current_user.email }}{% if is_admin %} · 管理員{% endif %}</span>
        <a href="/ui/logout">登出</a>
      </div>
    </aside>
    <label for="nav-toggle" class="scrim" aria-hidden="true"></label>
    <div class="main-col">
      <header class="topbar">
        <label for="nav-toggle" class="nav-burger" aria-label="切換選單"><span></span><span></span><span></span></label>
        <span class="topbar-brand">LINE Bot 管理</span>
      </header>
      <main>{% block content %}{% endblock %}</main>
    </div>
  </div>
  {% else %}
  <main>{% block content %}{% endblock %}</main>
  {% endif %}
  (AI widget 原樣保留於 body 尾)
</body>
```

注意:`side-link` 的 `active` 高亮全部比照上例 path 前綴寫齊(計畫省略處實作時補全);`/ui/profile` 前綴會誤中 `/ui/pos`?否——`startswith` 逐一精確寫;`/ui/booking` 涵蓋子頁。未登入頁(login 等)沒有 `current_user`,直接輸出 `<main>`(原本 header 就整塊 if 包住,行為一致)。

- [ ] **Step 2:整檔重寫 `app.css`**

依 Design Tokens 章 + 以下版面核心(其餘元件規則按 spec §2 的精確值展開):

```css
* { box-sizing: border-box; }
body { margin: 0; font-family: var(--font); background: var(--bg); color: var(--ink); line-height: 1.6; }

.layout { display: grid; grid-template-columns: 240px 1fr; min-height: 100vh; }
.sidebar { background: var(--card); border-right: 1px solid var(--line); padding: 20px 14px;
           display: flex; flex-direction: column; gap: 4px; position: sticky; top: 0;
           height: 100vh; overflow-y: auto; }
.side-brand { font-weight: 700; font-size: 17px; color: var(--brand); letter-spacing: .04em;
              padding: 4px 10px 16px; border-bottom: 1px solid var(--line); margin-bottom: 10px; }
.side-nav { flex: 1; display: flex; flex-direction: column; gap: 2px; }
.side-group summary { cursor: pointer; list-style: none; padding: 8px 10px; font-size: 12px;
                      font-weight: 700; color: var(--muted); letter-spacing: .08em; }
.side-group summary::-webkit-details-marker { display: none; }
.side-group summary::after { content: "›"; float: right; transition: transform .15s; }
.side-group[open] summary::after { transform: rotate(90deg); }
.side-link, .side-group a { display: block; padding: 8px 10px 8px 14px; border-radius: var(--radius-sm);
    color: var(--ink); text-decoration: none; font-size: 14px; border-left: 3px solid transparent; }
.side-group a { margin-left: 6px; }
.side-link:hover, .side-group a:hover { background: var(--brand-soft); }
.side-link.active, .side-group a.active { background: var(--brand-soft); color: var(--brand);
    font-weight: 600; border-left-color: var(--gold); }
.side-user { border-top: 1px solid var(--line); padding: 12px 10px 0; font-size: 13px; }
.topbar { display: none; }   /* 桌面隱藏,手機顯示 */
.scrim { display: none; }
main { max-width: 1080px; margin: 28px auto; padding: 0 28px; width: 100%; }
h1 { font-size: 22px; letter-spacing: .02em; margin: 0 0 6px; }
h1::after { content: ""; display: block; width: 36px; height: 3px; background: var(--gold);
            border-radius: 2px; margin-top: 10px; }
```

手機(≤768px):`.layout` 改單欄;`.sidebar` 改 `position: fixed; left: -260px; transition: left .2s; z-index: 40;`,`.nav-toggle:checked ~ .layout .sidebar { left: 0; }`;`.scrim` 於開啟時 `display:block` 全螢幕半透明;`.topbar { display:flex }` 帶漢堡(沿用三 span 動畫)。保留原 app.css:163-205 的表格塌縮與 16px input 規則,重繪配色。元件規則(`.card`/.btn/.badge/table/.stat/表單/`.error`/`.success`/`aiw-*` 配色)依 spec §2 精確值。舊別名 `--text/--border/--accent/--accent-dark` 必宣告。

- [ ] **Step 3:全量測試**

Run: `cd /root/saas-dev && bash run_tests.sh 2>&1 | tail -5`
Expected: `2100 passed`(± 與 main 基準相同;9 個 pip 環境性失敗可忽略)

- [ ] **Step 4:視覺驗證(Playwright 截圖)**

啟動:`cd /root/saas-dev && SAAS_ENV=test /opt/ti/.venv/bin/python -m saas_mvp &`(埠依 config,預設 8000)。
註冊/登入 demo 帳號後截:儀表板、預約管理、顧客、POS、報表、行事曆——桌面 1280px 與手機 375px 各一;確認側欄分組展開/高亮正確、表格塌縮正常、AI widget 配色。有錯位就修 CSS 再截。

- [ ] **Step 5:Commit + push + PR**

```bash
git add -A && git commit -m "feat(ui): 溫暖精品風 design system + 側邊欄分組導覽"
git push -u origin claude/ui-redesign
gh pr create --title "UI 重設計 1/4:design system + 側邊欄導覽" --body "..."
```

---

### Task 2(PR2):儀表板重設計

**Files:**
- Modify: `src/saas_mvp/templates/dashboard.html`
- Branch: `claude/ui-redesign-dashboard`(stacked on PR1 branch)

**Interfaces:**
- Consumes: Task 1 的 `.stat`/`.grid`/`.card` 樣式與新增輔助 class(如需要,於 app.css 增量:`.stat-hero`、`.pill`)。
- Produces: 無(葉子頁面)。

- [ ] **Step 1:重排 dashboard.html**

結構調整(context 變數/文案全不動):
1. 歡迎區:`<h1>{{ tenant.name }} 的儀表板</h1>` 保留;email 未驗證/試用提醒卡改用 `.card.notice-warn`(新 class,琥珀淡底左邊框)。
2. 「今日用量」「本月 LINE 經營」「本月推播用量」三張卡維持 `.grid`+`.stat`,不動內容。
3. 快速連結:`.btn` 海改成 `.quick-grid`(auto-fill minmax(120px,1fr) 的卡片式連結,`.quick-link` class;文字不動)。
4. LINE Bot 狀態與 `_settings.html` include 原樣。
app.css 增量加 `.notice-warn`、`.quick-grid`、`.quick-link`(hover 升 shadow-2)。

- [ ] **Step 2:全量測試** — 同 Task 1 Step 3。
- [ ] **Step 3:截圖驗證** — 儀表板桌面+手機。
- [ ] **Step 4:Commit + push + `gh pr create --base claude/ui-redesign`**(疊層,合併時 retarget;勿 --delete-branch)

---

### Task 3(PR3):顧客預約頁 + 公開店家頁 + pii

**Files:**
- Modify: `src/saas_mvp/templates/booking_form/form.html`(inline `<style>` 重寫)
- Modify: `src/saas_mvp/templates/booking_form/done.html`
- Modify: `src/saas_mvp/templates/public/profile.html`(inline `<style>` 重寫;`--theme` 機制保留)
- Modify: `src/saas_mvp/templates/pii/form.html`、`src/saas_mvp/templates/pii/done.html`
- Branch: `claude/ui-redesign-public`(直接基於 main,與 PR1 檔案集互斥、可獨立審)

**Interfaces:** 自帶 inline style(這些頁不載 app.css),token 值複製自 Design Tokens 章。

- [ ] **Step 1:booking_form/form.html** — 保留全部 Jinja 邏輯與文案;`<style>` 換精品風:暖白底、單欄 460px、`.choice`/`.slot-row` 改卡片式(hover 墨綠邊、選中 radio 時 `--brand-soft` 底)、按鈕墨綠、`.state`/`.error` 暖色化;h1 加琥珀底線。
- [ ] **Step 2:booking_form/done.html + pii 兩檔** — 同 token;done 頁墨綠大勾(CSS border 勾勿用圖檔)+ 摘要卡。「預約成功」等文案不動。
- [ ] **Step 3:public/profile.html** — 保留 `--theme: {{ profile.theme_color or ... }}`,預設值由 `#0a7cff` 改 `#1A5C4A`;banner/公告/tabs/卡片按精品風重繪(sticky tabs 保留);`_tab_*.html` 若含 inline style 同步調和,結構不動。
- [ ] **Step 4:全量測試** — booking_form/pii 有文案斷言測試,必須綠。
- [ ] **Step 5:截圖** — 預約流程 4 狀態(pick_service/pick_date/pick_slot/done)+ 公開頁,375px 為主(顧客幾乎全手機)。
- [ ] **Step 6:Commit + push + `gh pr create`(base=main)**

---

### Task 4(PR4):Auth + 員工入口 + admin 打磨

**Files:**
- Modify: `src/saas_mvp/templates/login.html`、`register.html`、`forgot_password.html`、`reset_password.html`、`join.html`(皆 extends base.html,只微調結構)
- Modify: `src/saas_mvp/templates/staff_portal/index.html`(獨立頁,inline style)
- Modify: `src/saas_mvp/templates/admin/overview.html`、`bots.html`、`audit.html`、`tenant_detail.html`(只在需要處加 class,樣式靠 PR1)
- Branch: `claude/ui-redesign-auth`(stacked on PR1)

**Interfaces:** Consumes Task 1 全部樣式;auth 頁用 `.auth-wrap`(PR1 已重繪:置中 420px、卡片 shadow-2、頂部品牌 logotype 字樣)。

- [ ] **Step 1:auth 五頁** — 加品牌 logotype 區塊(純文字 span.auth-brand,PR1 CSS 已定義或此處增量);表單結構/文案不動。
- [ ] **Step 2:staff_portal/index.html** — inline style 換 token。
- [ ] **Step 3:admin 四頁** — 檢視後僅調 class,不重排。
- [ ] **Step 4:全量測試 + 截圖(login/register/staff portal)**
- [ ] **Step 5:Commit + push + `gh pr create --base claude/ui-redesign`**

---

## Self-Review 紀錄

- Spec 覆蓋:§1 版面=Task1、§2 tokens=Task1、§3 對外頁=Task3+4、§5 PR 切分=四 Task 對應。無缺。
- 型別/命名一致:`.side-link`/`.side-group`/`.nav-toggle`/`.scrim`/`.topbar` 在 Task1 CSS 與 HTML 對齊;Task2 新 class 於 app.css 增量,不與 Task1 衝突。
- 已修正:舊 CSS 變數名(--text/--border/--accent)可能被模板 inline style 引用 → 加相容別名。
