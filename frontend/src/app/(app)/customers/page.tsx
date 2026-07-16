"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";

import { fetchList } from "@/lib/client-api";

export type CustomerRow = {
  id: number;
  line_user_id: string;
  display_name: string | null;
  phone: string | null;
  booking_count: number;
  last_booked_at: string | null;
  points_balance: number;
  tier: string;
  blacklisted: boolean;
};

const PAGE_SIZE = 30;

export default function CustomersPage() {
  const [q, setQ] = useState("");
  const [applied, setApplied] = useState("");
  const [page, setPage] = useState(0);

  const query = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(page * PAGE_SIZE) });
  if (applied) query.set("q", applied);

  const { data, isLoading } = useQuery({
    queryKey: ["customers", query.toString()],
    queryFn: () => fetchList<CustomerRow>(`/booking/customers/?${query}`),
  });

  const totalPages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1;

  return (
    <div className="mx-auto max-w-6xl">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">顧客</h1>
        <a href="/ui/customers" className="text-sm text-muted hover:text-ink">
          進階(匯入/分眾/標籤管理)→ 舊版頁面
        </a>
      </header>

      <form
        className="mt-4 flex gap-2 text-sm"
        onSubmit={(e) => { e.preventDefault(); setApplied(q.trim()); setPage(0); }}
      >
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="姓名或電話搜尋"
          className="w-64 rounded-lg border border-line bg-surface px-3 py-2"
        />
        <button className="rounded-lg bg-brand px-4 py-2 font-semibold text-white hover:bg-brand-deep">
          搜尋
        </button>
        {data && <p className="ml-auto self-center text-muted">共 {data.total} 位</p>}
      </form>

      <div className="mt-4 overflow-x-auto rounded-xl border border-line bg-surface">
        <table className="w-full min-w-[640px] text-sm">
          <thead>
            <tr className="border-b border-line text-left text-muted">
              <th className="px-4 py-2.5 font-medium">顧客</th>
              <th className="px-4 py-2.5 font-medium">電話</th>
              <th className="px-4 py-2.5 font-medium">預約數</th>
              <th className="px-4 py-2.5 font-medium">最近預約</th>
              <th className="px-4 py-2.5 font-medium">點數</th>
              <th className="px-4 py-2.5 font-medium">等級</th>
            </tr>
          </thead>
          <tbody>
            {isLoading && (
              <tr><td colSpan={6} className="px-4 py-8 text-center text-muted">載入中…</td></tr>
            )}
            {data?.rows.length === 0 && (
              <tr><td colSpan={6} className="px-4 py-8 text-center text-muted">找不到顧客。</td></tr>
            )}
            {data?.rows.map((c) => (
              <tr key={c.id} className="border-b border-line/60">
                <td className="px-4 py-2.5">
                  <Link href={`/customers/${c.id}`} className="font-medium text-brand hover:underline">
                    {c.display_name ?? `顧客 #${c.id}`}
                  </Link>
                  {c.blacklisted && (
                    <span className="ml-2 rounded-full bg-danger-soft px-2 py-0.5 text-xs text-danger">黑名單</span>
                  )}
                </td>
                <td className="px-4 py-2.5">{c.phone ?? "—"}</td>
                <td className="px-4 py-2.5">{c.booking_count}</td>
                <td className="px-4 py-2.5 text-muted">
                  {c.last_booked_at ? c.last_booked_at.slice(0, 10) : "—"}
                </td>
                <td className="px-4 py-2.5">{c.points_balance}</td>
                <td className="px-4 py-2.5">{c.tier}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="mt-3 flex items-center justify-end gap-2 text-sm">
        <button disabled={page === 0} onClick={() => setPage((p) => p - 1)}
          className="rounded-md border border-line px-3 py-1 disabled:opacity-40">上一頁</button>
        <span className="text-muted">{page + 1} / {totalPages}</span>
        <button disabled={page + 1 >= totalPages} onClick={() => setPage((p) => p + 1)}
          className="rounded-md border border-line px-3 py-1 disabled:opacity-40">下一頁</button>
      </div>
    </div>
  );
}
