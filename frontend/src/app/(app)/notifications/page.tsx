"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, fetchJson, fetchList } from "@/lib/client-api";

type BookingNotifRow = {
  id: number;
  reservation_id: number | null;
  kind: string;
  status: string;
  payload_text: string;
  sent_at: string | null;
  attempt_count: number;
  last_error: string | null;
  created_at: string;
};

type CampaignSendRow = {
  id: number;
  campaign_id: number;
  customer_id: number | null;
  status: string;
  period_key: string;
  sent_at: string | null;
  attempt_count: number;
  last_error: string | null;
  created_at: string;
};

type PushUsageRow = { period: string; used: number };

const PAGE = 50;

const KIND_LABELS: Record<string, string> = {
  change: "改期通知",
  cancel: "取消通知",
  deposit_refund: "退款通知",
};

function statusBadge(status: string) {
  const cls =
    status === "sent"
      ? "bg-ok-soft text-ok"
      : status === "pending"
        ? "bg-warn-soft text-warn"
        : "bg-danger-soft text-danger";
  return <span className={`rounded-full px-2 py-0.5 text-xs ${cls}`}>{status}</span>;
}

function errText(error: unknown): string {
  if (error instanceof ApiError) return error.detail || `錯誤(${error.status})`;
  return "載入失敗,請重試。";
}

function Pager({ page, total, onPage }: { page: number; total: number; onPage: (p: number) => void }) {
  const pages = Math.max(1, Math.ceil(total / PAGE));
  if (pages <= 1) return null;
  return (
    <div className="mt-3 flex items-center gap-2 text-sm">
      <button disabled={page <= 0} onClick={() => onPage(page - 1)}
        className="rounded-md border border-line px-2 py-1 disabled:opacity-40">上一頁</button>
      <span className="text-muted">{page + 1} / {pages}(共 {total} 筆)</span>
      <button disabled={page >= pages - 1} onClick={() => onPage(page + 1)}
        className="rounded-md border border-line px-2 py-1 disabled:opacity-40">下一頁</button>
    </div>
  );
}

function BookingsTab() {
  const [page, setPage] = useState(0);
  const [status, setStatus] = useState("");
  const q = useQuery({
    queryKey: ["notif-bookings", page, status],
    queryFn: () =>
      fetchList<BookingNotifRow>(
        `/api/v1/notifications/bookings?limit=${PAGE}&offset=${page * PAGE}${status ? `&status=${status}` : ""}`,
      ),
    retry: false,
  });
  return (
    <div>
      <div className="flex items-center gap-2 text-sm">
        <label className="text-muted">狀態</label>
        <select value={status} onChange={(e) => { setStatus(e.target.value); setPage(0); }}
          className="rounded-lg border border-line px-2 py-1.5">
          <option value="">全部</option>
          <option value="pending">pending</option>
          <option value="sent">sent</option>
          <option value="failed">failed</option>
          <option value="skipped">skipped</option>
        </select>
      </div>
      {q.error && <p className="mt-3 text-sm text-danger">{errText(q.error)}</p>}
      <div className="mt-3 overflow-x-auto rounded-xl border border-line bg-surface">
        <table className="w-full min-w-[640px] text-sm">
          <thead>
            <tr className="border-b border-line text-left text-muted">
              <th className="px-4 py-2.5 font-medium">時間</th>
              <th className="px-4 py-2.5 font-medium">類型</th>
              <th className="px-4 py-2.5 font-medium">預約</th>
              <th className="px-4 py-2.5 font-medium">內容</th>
              <th className="px-4 py-2.5 font-medium">狀態</th>
            </tr>
          </thead>
          <tbody>
            {q.isLoading && (
              <tr><td colSpan={5} className="px-4 py-8 text-center text-muted">載入中…</td></tr>
            )}
            {q.data?.rows.length === 0 && (
              <tr><td colSpan={5} className="px-4 py-8 text-center text-muted">尚無通知紀錄。</td></tr>
            )}
            {q.data?.rows.map((r) => (
              <tr key={r.id} className="border-b border-line/60 align-top">
                <td className="px-4 py-2.5 whitespace-nowrap text-xs">
                  {(r.sent_at ?? r.created_at).slice(0, 16).replace("T", " ")}
                </td>
                <td className="px-4 py-2.5">{KIND_LABELS[r.kind] ?? r.kind}</td>
                <td className="px-4 py-2.5">{r.reservation_id ? `#${r.reservation_id}` : "—"}</td>
                <td className="max-w-[340px] px-4 py-2.5">
                  <span className="line-clamp-2 text-xs">{r.payload_text}</span>
                  {r.last_error && <span className="block text-xs text-danger">{r.last_error}</span>}
                </td>
                <td className="px-4 py-2.5">{statusBadge(r.status)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Pager page={page} total={q.data?.total ?? 0} onPage={setPage} />
    </div>
  );
}

function CampaignSendsTab() {
  const [page, setPage] = useState(0);
  const q = useQuery({
    queryKey: ["notif-campaign-sends", page],
    queryFn: () =>
      fetchList<CampaignSendRow>(
        `/api/v1/notifications/campaign-sends?limit=${PAGE}&offset=${page * PAGE}`,
      ),
    retry: false,
  });
  return (
    <div>
      {q.error && <p className="mt-1 text-sm text-danger">{errText(q.error)}</p>}
      <div className="mt-3 overflow-x-auto rounded-xl border border-line bg-surface">
        <table className="w-full min-w-[560px] text-sm">
          <thead>
            <tr className="border-b border-line text-left text-muted">
              <th className="px-4 py-2.5 font-medium">時間</th>
              <th className="px-4 py-2.5 font-medium">活動</th>
              <th className="px-4 py-2.5 font-medium">顧客</th>
              <th className="px-4 py-2.5 font-medium">期別</th>
              <th className="px-4 py-2.5 font-medium">狀態</th>
            </tr>
          </thead>
          <tbody>
            {q.isLoading && (
              <tr><td colSpan={5} className="px-4 py-8 text-center text-muted">載入中…</td></tr>
            )}
            {q.data?.rows.length === 0 && (
              <tr><td colSpan={5} className="px-4 py-8 text-center text-muted">尚無發送紀錄。</td></tr>
            )}
            {q.data?.rows.map((r) => (
              <tr key={r.id} className="border-b border-line/60">
                <td className="px-4 py-2.5 whitespace-nowrap text-xs">
                  {(r.sent_at ?? r.created_at).slice(0, 16).replace("T", " ")}
                </td>
                <td className="px-4 py-2.5">#{r.campaign_id}</td>
                <td className="px-4 py-2.5">{r.customer_id ? `#${r.customer_id}` : "—"}</td>
                <td className="px-4 py-2.5 text-xs">{r.period_key}</td>
                <td className="px-4 py-2.5">
                  {statusBadge(r.status)}
                  {r.last_error && <span className="block text-xs text-danger">{r.last_error}</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Pager page={page} total={q.data?.total ?? 0} onPage={setPage} />
    </div>
  );
}

function PushUsageTab() {
  const q = useQuery({
    queryKey: ["notif-push-usage"],
    queryFn: () => fetchJson<PushUsageRow[]>("/api/v1/notifications/push-usage?months=6"),
    retry: false,
  });
  const max = Math.max(1, ...(q.data ?? []).map((r) => r.used));
  return (
    <div className="mt-3 rounded-xl border border-line bg-surface p-4">
      {q.error && <p className="text-sm text-danger">{errText(q.error)}</p>}
      {q.isLoading && <p className="text-sm text-muted">載入中…</p>}
      <ul className="grid gap-2 text-sm">
        {q.data?.map((r) => (
          <li key={r.period} className="flex items-center gap-3">
            <span className="w-20 shrink-0 text-muted">
              {r.period.slice(0, 4)}-{r.period.slice(4)}
            </span>
            <div className="h-3 rounded bg-brand" style={{ width: `${(r.used / max) * 70}%`, minWidth: r.used ? 4 : 0 }} />
            <span>{r.used}</span>
          </li>
        ))}
      </ul>
      <p className="mt-3 text-xs text-muted">月度推播計量(行銷+通知共用額度);額度加購請至進階功能。</p>
    </div>
  );
}

export default function NotificationsPage() {
  const [tab, setTab] = useState<"bookings" | "campaigns" | "usage">("bookings");
  const tabs = [
    { key: "bookings" as const, label: "預約通知" },
    { key: "campaigns" as const, label: "行銷發送" },
    { key: "usage" as const, label: "推播用量" },
  ];
  return (
    <div className="mx-auto max-w-5xl">
      <h1 className="text-2xl font-semibold">通知歷程</h1>
      <div className="mt-4 flex gap-1 border-b border-line text-sm">
        {tabs.map((t) => (
          <button key={t.key} onClick={() => setTab(t.key)}
            className={`rounded-t-lg px-4 py-2 ${
              tab === t.key
                ? "border border-b-0 border-line bg-surface font-semibold text-ink"
                : "text-muted hover:text-ink"
            }`}>
            {t.label}
          </button>
        ))}
      </div>
      <div className="mt-4">
        {tab === "bookings" && <BookingsTab />}
        {tab === "campaigns" && <CampaignSendsTab />}
        {tab === "usage" && <PushUsageTab />}
      </div>
    </div>
  );
}
