"use client";

import type { ReactNode, Key } from "react";

/** 欄位定義驅動的表格原語(R6-D1)。沿用既有 console 表格 Tailwind 樣式,
 * 取代各頁手刻 <table>。純展示(狀態/資料由呼叫端 react-query 提供)。 */
export type Column<T> = {
  header: string;
  cell: (row: T) => ReactNode;
  /** td 額外 class(對齊/寬度等)。 */
  className?: string;
  /** th 額外 class。 */
  headerClassName?: string;
};

export function DataTable<T>({
  columns,
  rows,
  rowKey,
  isLoading = false,
  emptyText = "沒有資料。",
  loadingText = "載入中…",
  minWidth = 640,
  onRowClick,
}: {
  columns: Column<T>[];
  rows: T[] | undefined;
  rowKey: (row: T) => Key;
  isLoading?: boolean;
  emptyText?: string;
  loadingText?: string;
  minWidth?: number;
  onRowClick?: (row: T) => void;
}) {
  const span = columns.length;
  return (
    <div className="overflow-x-auto rounded-xl border border-line bg-surface">
      <table className="w-full text-sm" style={{ minWidth }}>
        <thead>
          <tr className="border-b border-line text-left text-muted">
            {columns.map((c, i) => (
              <th key={i} className={`px-4 py-2.5 font-medium ${c.headerClassName ?? ""}`}>
                {c.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {isLoading && (
            <tr>
              <td colSpan={span} className="px-4 py-8 text-center text-muted">
                {loadingText}
              </td>
            </tr>
          )}
          {!isLoading && rows?.length === 0 && (
            <tr>
              <td colSpan={span} className="px-4 py-8 text-center text-muted">
                {emptyText}
              </td>
            </tr>
          )}
          {!isLoading &&
            rows?.map((row) => (
              <tr
                key={rowKey(row)}
                className={`border-b border-line/60 ${onRowClick ? "cursor-pointer hover:bg-line/20" : ""}`}
                onClick={onRowClick ? () => onRowClick(row) : undefined}
              >
                {columns.map((c, i) => (
                  <td key={i} className={`px-4 py-2.5 ${c.className ?? ""}`}>
                    {c.cell(row)}
                  </td>
                ))}
              </tr>
            ))}
        </tbody>
      </table>
    </div>
  );
}
