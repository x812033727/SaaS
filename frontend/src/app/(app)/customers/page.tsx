"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";

import { fetchList } from "@/lib/client-api";
import { pageOffset } from "@/lib/pagination";
import { DataTable, type Column } from "@/components/ui/DataTable";
import { Pager } from "@/components/ui/Pager";

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

const columns: Column<CustomerRow>[] = [
  {
    header: "顧客",
    cell: (c) => (
      <>
        <Link href={`/customers/${c.id}`} className="font-medium text-brand hover:underline">
          {c.display_name ?? `顧客 #${c.id}`}
        </Link>
        {c.blacklisted && (
          <span className="ml-2 rounded-full bg-danger-soft px-2 py-0.5 text-xs text-danger">黑名單</span>
        )}
      </>
    ),
  },
  { header: "電話", cell: (c) => c.phone ?? "—" },
  { header: "預約數", cell: (c) => c.booking_count },
  { header: "最近預約", className: "text-muted", cell: (c) => (c.last_booked_at ? c.last_booked_at.slice(0, 10) : "—") },
  { header: "點數", cell: (c) => c.points_balance },
  { header: "等級", cell: (c) => c.tier },
];

export default function CustomersPage() {
  const [q, setQ] = useState("");
  const [applied, setApplied] = useState("");
  const [page, setPage] = useState(0);

  const query = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(pageOffset(page, PAGE_SIZE)) });
  if (applied) query.set("q", applied);

  const { data, isLoading } = useQuery({
    queryKey: ["customers", query.toString()],
    queryFn: () => fetchList<CustomerRow>(`/booking/customers/?${query}`),
  });

  return (
    <div className="mx-auto max-w-6xl">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">顧客</h1>
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

      <div className="mt-4">
        <DataTable
          columns={columns}
          rows={data?.rows}
          rowKey={(c) => c.id}
          isLoading={isLoading}
          emptyText="找不到顧客。"
        />
      </div>

      <Pager page={page} total={data?.total ?? 0} pageSize={PAGE_SIZE} onPageChange={setPage} />
    </div>
  );
}
