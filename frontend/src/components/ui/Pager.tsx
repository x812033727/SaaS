"use client";

import { pageCount } from "@/lib/pagination";

/** 分頁控制原語(R6-D1)。上一頁/下一頁 + 頁碼指示;沿用既有 console 樣式。
 * page 為 0-based;total/pageSize 由呼叫端提供,內部算總頁數。 */
export function Pager({
  page,
  total,
  pageSize,
  onPageChange,
}: {
  page: number;
  total: number;
  pageSize: number;
  onPageChange: (page: number) => void;
}) {
  const totalPages = pageCount(total, pageSize);
  return (
    <div className="mt-3 flex items-center justify-end gap-2 text-sm">
      <button
        type="button"
        disabled={page <= 0}
        onClick={() => onPageChange(page - 1)}
        className="rounded-md border border-line px-3 py-1 disabled:opacity-40"
      >
        上一頁
      </button>
      <span className="text-muted">
        {page + 1} / {totalPages}
      </span>
      <button
        type="button"
        disabled={page + 1 >= totalPages}
        onClick={() => onPageChange(page + 1)}
        className="rounded-md border border-line px-3 py-1 disabled:opacity-40"
      >
        下一頁
      </button>
    </div>
  );
}
