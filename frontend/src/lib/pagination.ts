/** 分頁純邏輯(R6-D1)— node 可單測,DataTable/Pager 共用。 */

/** 總頁數;total<=0 或 pageSize<=0 時回 1(至少一頁,避免除零/空狀態閃動)。 */
export function pageCount(total: number, pageSize: number): number {
  if (pageSize <= 0) return 1;
  return Math.max(1, Math.ceil(Math.max(0, total) / pageSize));
}

/** 夾住頁碼於 [0, totalPages-1];用於 total 變動後頁碼越界回收。 */
export function clampPage(page: number, totalPages: number): number {
  const last = Math.max(0, totalPages - 1);
  if (page < 0) return 0;
  if (page > last) return last;
  return page;
}

/** offset(0-based page × pageSize),供 API query。 */
export function pageOffset(page: number, pageSize: number): number {
  return Math.max(0, page) * Math.max(0, pageSize);
}
