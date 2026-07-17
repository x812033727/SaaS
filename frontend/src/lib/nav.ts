/** 側欄導覽設定 — 同構 /ui base.html 的分組;migrated=true 走 console 頁(Next Link),
 * 其餘以 <a href="/ui/..."> 連回舊後台(同網域、同 JWT cookie,免二次登入)。
 * adminOnly 群組依 /api/v1/context 的 permissions 顯示。 */

export type NavItem = {
  label: string;
  href: string;
  migrated: boolean;
};

export type NavGroup = {
  label: string;
  items: NavItem[];
  adminOnly?: boolean;
};

export const NAV_TOP: NavItem[] = [
  { label: "儀表板", href: "/dashboard", migrated: true },
  { label: "舊版後台", href: "/ui/", migrated: false },
];

export const NAV_GROUPS: NavGroup[] = [
  {
    label: "預約營運",
    items: [
      { label: "預約管理", href: "/reservations", migrated: true },
      { label: "行事曆", href: "/calendar", migrated: true },
      { label: "時段管理", href: "/slots", migrated: true },
      { label: "顧客", href: "/customers", migrated: true },
      { label: "諮詢表／同意書", href: "/ui/client-forms", migrated: false },
      { label: "房間／設備", href: "/ui/resources", migrated: false },
      { label: "備註", href: "/ui/notes", migrated: false },
    ],
  },
  {
    label: "LINE",
    items: [
      { label: "LINE 設定", href: "/line-settings", migrated: true },
      { label: "客服訊息", href: "/ui/line-chat", migrated: false },
      { label: "自動回覆", href: "/ui/auto-reply", migrated: false },
      { label: "圖文選單", href: "/ui/rich-menu", migrated: false },
      { label: "圖文卡片", href: "/ui/flex-menu", migrated: false },
      { label: "AI 客服", href: "/ui/faq", migrated: false },
    ],
  },
  {
    label: "商店銷售",
    items: [
      { label: "服務項目", href: "/services", migrated: true },
      { label: "服務套票", href: "/ui/packages", migrated: false },
      { label: "電子禮物卡", href: "/ui/gift-cards", migrated: false },
      { label: "POS 結帳", href: "/pos", migrated: true },
      { label: "商品", href: "/shop", migrated: true },
      { label: "優惠券", href: "/coupons", migrated: true },
      { label: "分店", href: "/locations", migrated: true },
      { label: "員工", href: "/staff", migrated: true },
      { label: "抽成／薪資", href: "/ui/commissions", migrated: false },
    ],
  },
  {
    label: "行銷",
    items: [
      { label: "行銷活動", href: "/ui/campaigns", migrated: false },
      { label: "通知歷程", href: "/ui/notifications", migrated: false },
      { label: "作品集", href: "/ui/portfolio", migrated: false },
      { label: "店家頁", href: "/ui/profile", migrated: false },
    ],
  },
  {
    label: "報表",
    items: [
      { label: "營運報表", href: "/reports", migrated: true },
      { label: "進階報表/匯出", href: "/ui/reports", migrated: false },
    ],
  },
  {
    label: "帳務設定",
    items: [
      { label: "方案", href: "/ui/plan", migrated: false },
      { label: "帳單", href: "/ui/billing", migrated: false },
      { label: "進階功能", href: "/ui/features", migrated: false },
      { label: "API 金鑰", href: "/ui/api-keys", migrated: false },
      { label: "成員", href: "/ui/members", migrated: false },
      { label: "帳號", href: "/ui/account", migrated: false },
    ],
  },
  // 平台管理(admin)頁本輪不搬:context.permissions 是組織角色權限、不含
  // is_admin 平台旗標;admin 從「舊版後台」入口使用 /ui/admin/*。
];
