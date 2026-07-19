"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRef, useState } from "react";

import { ApiError, fetchList, postJson } from "@/lib/client-api";
import { pageOffset } from "@/lib/pagination";
import { DataTable, type Column } from "@/components/ui/DataTable";
import { Pager } from "@/components/ui/Pager";

export type CustomerRow = {
  id: number;
  // NULL = walk-in/網路預約客(無 LINE 身分)
  line_user_id: string | null;
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

type ImportReport = {
  created: number; updated: number; skipped: number; errors: string[]; ok: boolean;
};

export default function CustomersPage() {
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const [applied, setApplied] = useState("");
  const [page, setPage] = useState(0);
  const [report, setReport] = useState<ImportReport | null>(null);
  const [importError, setImportError] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  const updateExistingRef = useRef<HTMLInputElement>(null);

  // R12-C2:CSV 匯入(/ui 退役後只剩此入口)。內容以 UTF-8 字串上傳。
  const importMut = useMutation({
    mutationFn: (input: { content: string; update_existing: boolean }) =>
      postJson<ImportReport>("/api/v1/customers/import", input),
    onSuccess: (r) => {
      setReport(r);
      setImportError("");
      if (r.ok) qc.invalidateQueries({ queryKey: ["customers"] });
    },
    onError: (e) =>
      setImportError(e instanceof ApiError ? e.detail || `錯誤(${e.status})` : "匯入失敗,請重試。"),
  });

  function submitImport() {
    const file = fileRef.current?.files?.[0];
    if (!file) { setImportError("請先選擇 CSV 檔案。"); return; }
    const reader = new FileReader();
    reader.onload = () =>
      importMut.mutate({
        content: String(reader.result ?? ""),
        update_existing: Boolean(updateExistingRef.current?.checked),
      });
    reader.onerror = () => setImportError("讀取檔案失敗。");
    reader.readAsText(file, "utf-8");
  }

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
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <a href="/console/api/proxy/booking/customers/export.csv"
            className="rounded-lg border border-line px-3 py-1.5 hover:bg-brand-soft">
            匯出 CSV
          </a>
          <input ref={fileRef} type="file" accept=".csv,text/csv" className="text-xs" />
          <label className="flex items-center gap-1">
            <input ref={updateExistingRef} type="checkbox" />
            更新既有
          </label>
          <button onClick={submitImport} disabled={importMut.isPending}
            className="rounded-lg bg-brand px-3 py-1.5 font-semibold text-white hover:bg-brand-deep disabled:opacity-60">
            {importMut.isPending ? "匯入中…" : "匯入 CSV"}
          </button>
        </div>
      </header>

      {importError && (
        <p className="mt-3 rounded-lg bg-danger-soft px-3 py-2 text-sm text-danger">{importError}</p>
      )}
      {report && (
        <div className={`mt-3 rounded-lg px-3 py-2 text-sm ${report.ok ? "bg-ok-soft text-ok" : "bg-danger-soft text-danger"}`}>
          {report.ok
            ? `匯入完成:新增 ${report.created}、更新 ${report.updated}、略過 ${report.skipped}。`
            : `匯入失敗(整批未寫入):`}
          {!report.ok && (
            <ul className="mt-1 list-disc pl-5">
              {report.errors.slice(0, 10).map((e, i) => <li key={i}>{e}</li>)}
            </ul>
          )}
        </div>
      )}

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
