# UI 全面重設計(溫暖精品風)設計文件

日期:2026-07-11
狀態:已與使用者確認方向(全面重設計/溫暖精品風/涵蓋全部頁面)

## 目標

把「LINE 綠工具風」的陽春介面,升級為符合 399/899 訂閱定位的精品管理平台:

1. 解決導覽混亂:頂欄 28 個平鋪連結 → 側邊欄 8 個分組。
2. 建立完整視覺系統(design tokens),取代零散的顏色/間距硬編碼。
3. 顧客可見頁面(預約表單、公開店家頁)做出品牌門面感。

## 非目標

- 不改任何後端邏輯、路由、表單欄位、htmx 行為。
- 不引入 JS 框架或 build step;維持 Jinja + htmx + 單檔 CSS。
- 不做深色模式(暖色精品風 v1 不需要;token 化後未來可加)。
- 不改任何使用者可見文案(測試斷言中文文案,如「已儲存」「尚未開通」「預約成功」)。

## 實作策略:換皮不換骨

現有 70+ 模板共用同一組 class(`.card` `.btn` `.badge` `.stat` `.grid` `.row` `table`)。
**沿用既有 class 名、重寫其樣式**,讓大多數模板零改動;只有結構需要升級的頁面才動 HTML:

- `base.html`:頂欄導覽 → 側邊欄分組(結構重寫)
- `dashboard.html`:資訊層次重排
- `booking_form/`、`public/`、`pii/`:對外頁獨立版面
- auth 頁(login/register/forgot/reset/join)、`staff_portal/`、`admin/`:小幅打磨

htmx partial(`_*.html`)回傳片段只依賴 class 樣式,不需逐一改動。

## 1. 版面架構

### 桌面(>768px)

- 固定左側欄 240px:品牌區 + 分組導覽 + 使用者區(email/登出)。
- 主內容區:頂部細條(目前頁面標題、代管橫幅保留於最上方)+ 內容 max-width 1080px。
- 側欄分組用 `<details>/<summary>` 收合(CSS-only);目前頁面所在群組以 Jinja 依
  `request.url.path` 前綴自動加 `open`,目前項目高亮(左側 3px 琥珀金指示條)。

### 手機(≤768px)

- 側欄變全高抽屜(checkbox toggle + 半透明遮罩,維持 CSS-only)。
- 頂欄:漢堡 + 品牌名 + 帳號連結。
- 既有的「表格塌縮成卡片(data-label)」機制保留,重繪樣式。

### 導覽分組(28 → 8 組)

| 組 | 項目 |
|---|---|
| 儀表板 | 儀表板(單項,不收合) |
| 預約營運 | 預約管理、行事曆、顧客、備註 |
| LINE | LINE 設定、客服訊息、自動回覆、圖文選單、圖文卡片、AI 客服 |
| 商店銷售 | 服務項目、POS 結帳、商品、優惠券、分店、員工 |
| 行銷 | 行銷活動、通知歷程、作品集、店家頁 |
| 報表 | 報表(單項) |
| 帳務設定 | 方案、帳單、進階功能、API 金鑰、成員、帳號 |
| 平台管理 | 平台總覽、跨店總覽、稽核(僅 admin 顯示) |

連結文字與 href 一字不動,只改容器結構。

## 2. 視覺系統(design tokens)

```css
:root {
  /* 色彩 */
  --bg: #FAF9F6;            /* 暖白底 */
  --card: #FFFFFF;
  --ink: #2B2B28;           /* 暖黑文字 */
  --muted: #8A867C;         /* 暖灰次要文字 */
  --line: #E9E5DD;          /* 暖色邊框 */
  --brand: #1A5C4A;         /* 深墨綠(主按鈕、連結、強調) */
  --brand-deep: #124436;
  --brand-soft: #E8F0EC;    /* 墨綠淡底(hover、selected) */
  --gold: #C4956A;          /* 琥珀金點綴(指示條、重點數字) */
  --gold-soft: #F6EEE3;
  --danger: #B3564D;        /* 暖紅 */
  --ok: #3E7D5F;
  --warn: #B7791F;
  /* 形狀與層次 */
  --radius: 12px;  --radius-sm: 8px;
  --shadow-1: 0 1px 2px rgba(43,43,40,.05), 0 2px 8px rgba(43,43,40,.04);
  --shadow-2: 0 4px 16px rgba(43,43,40,.08);
}
```

- 字體:`"Noto Sans TC", -apple-system, ...`;h1/h2 加 `letter-spacing: .02em`,頁面標題下加琥珀金短底線裝飾。
- 元件重繪(class 名不變):
  - `.card`:白底、12px 圓角、`--shadow-1`、hover 不動(非互動容器)。
  - `.btn`/`button`:主=墨綠、secondary=暖灰底墨字、danger=暖紅;皆有 hover 深化 + `:active` 微縮。
  - `.badge`:狀態色改暖色系(valid/on=墨綠淡底、invalid/error=暖紅淡底、unchecked=琥珀淡底、none=暖灰)。
  - `table`:表頭小型大寫化(中文用字重+字距)、row hover 用 `--brand-soft`、儲存格留白加大。
  - `.stat`:統計卡升級——數字 28px 墨綠、label 上移、卡底細琥珀線。
  - 表單控件:focus ring 用 `--brand` 外暈;label 字重上調。
- AI 客服浮動 widget(`aiw-*`):配色隨 token 改墨綠,行為不動。

## 3. 對外頁面

不繼承 `base.html`,各自輕量版面 + 同一套 token(獨立 `<style>` 或共用 `app.css`):

- **顧客預約表單(booking_form/form.html + done.html)**:單欄卡片流程、店名做視覺主角、
  完成頁給確認感(墨綠大勾 + 預約摘要卡)。文案不動(「開始預約」「請選擇時段」「預約成功」被測試斷言)。
- **公開店家頁(public/profile.html + tabs)**:門面感——店名橫幅、tab 導覽精緻化、
  服務/作品集卡片網格。
- **Auth 頁**:置中卡片、品牌 logotype、精緻表單。
- **staff_portal / pii**:套 token 的簡潔卡片版面。

## 4. 相容性硬約束

1. 使用者可見文案一字不動(全量測試斷言文案)。
2. 表單 name/action/method、htmx 屬性(hx-*)、id 一律不動。
3. 不新增 JS;互動維持 CSS-only(checkbox/details)。
4. `data-label` 手機表格機制保留。
5. 無 migration、無後端 Python 改動。側欄高亮用 `request.url.path` 前綴比對——
   已查證所有 UI 頁面經 `templates.TemplateResponse(..., _ctx(request))` 渲染,
   FastAPI Jinja2Templates 必含 `request` 於 context。

## 5. 交付切分(4 PR,依序疊加但各自獨立可審)

1. **PR1 design-system**:重寫 `app.css` + `base.html` 側邊欄。視覺翻轉的主體,影響所有 /ui 頁。
2. **PR2 dashboard**:`dashboard.html` 資訊層次重排(歡迎列、統計卡組、近期預約卡片化)。
3. **PR3 對外頁**:booking_form / public / pii。
4. **PR4 auth+staff+admin**:login 系列、staff_portal、admin 頁打磨。

每支 PR:全量 `bash run_tests.sh` 綠 + 本地起服務用 Playwright 截圖(桌面 + 375px 手機)人工目檢。

## 6. 風險與對策

- **測試假設破裂**:若有測試斷言 markup(目前掃描為零),以文案為準修 markup,不改文案。
- **htmx partial 在新版面錯位**:PR1 完成後用截圖巡檢主要頁面(儀表板、預約、顧客、POS、報表)。
- **squash 疊層 PR conflict**:各 PR 檔案集互斥(PR1=css+base、PR2=dashboard、PR3=對外、PR4=auth/admin),理論上零 conflict;若動同檔,解完必 grep conflict 標記。
