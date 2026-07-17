"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, fetchJson, postJson } from "@/lib/client-api";

type CampaignRow = {
  id: number;
  name: string;
  type: string;
  status: string;
  schedule_at: string | null;
  expires_at: string | null;
  reward_type: string | null;
  reward_value: number | null;
  message_template: string;
  is_active: boolean;
};

const TYPE_LABELS: Record<string, string> = {
  birthday: "生日祝福",
  welcome: "新客歡迎",
  spend: "消費回饋",
  reactivation: "喚回沉睡客",
  broadcast: "廣播",
};

function errText(error: unknown): string {
  if (error instanceof ApiError) return error.detail || `錯誤(${error.status})`;
  return "操作失敗,請重試。";
}

function FeatureLockedCard() {
  return (
    <div className="rounded-xl border border-line bg-warn-soft p-6 text-sm">
      <p className="font-semibold text-warn">此功能未啟用</p>
      <p className="mt-2 text-ink">
        行銷活動屬進階功能,請至
        <a href="/ui/plan" className="mx-1 text-brand underline">方案頁</a>
        升級或啟用後再試。
      </p>
    </div>
  );
}

export default function CampaignsPage() {
  const qc = useQueryClient();
  const [editing, setEditing] = useState<CampaignRow | null>(null);
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  const campaigns = useQuery({
    queryKey: ["campaigns-admin"],
    queryFn: () => fetchJson<CampaignRow[]>("/booking/campaigns/"),
    retry: false,
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["campaigns-admin"] });

  const saveMut = useMutation({
    mutationFn: async (input: { id: number | null; body: Record<string, unknown> }) =>
      input.id === null
        ? postJson("/booking/campaigns/", input.body)
        : fetchJson(`/booking/campaigns/${input.id}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(input.body),
          }),
    onSuccess: () => { invalidate(); setEditing(null); setMessage({ kind: "ok", text: "已儲存。" }); },
    onError: (e) => setMessage({ kind: "error", text: errText(e) }),
  });

  const runMut = useMutation({
    mutationFn: (id: number) =>
      postJson<{ sent: number; skipped: number }>(`/booking/campaigns/${id}/run`, {}),
    onSuccess: (r) => {
      invalidate();
      setMessage({ kind: "ok", text: `已執行:發送 ${r.sent} 筆、略過 ${r.skipped} 筆。` });
    },
    onError: (e) => setMessage({ kind: "error", text: errText(e) }),
  });

  if (campaigns.error instanceof ApiError && campaigns.error.status === 403) {
    return (
      <div className="mx-auto max-w-5xl">
        <h1 className="text-2xl font-semibold">行銷活動</h1>
        <div className="mt-6"><FeatureLockedCard /></div>
      </div>
    );
  }

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const schedule = String(form.get("schedule_at") ?? "").trim();
    const expires = String(form.get("expires_at") ?? "").trim();
    const rewardType = String(form.get("reward_type") ?? "");
    const rewardValue = String(form.get("reward_value") ?? "").trim();
    saveMut.mutate({
      id: editing?.id ?? null,
      body: {
        name: String(form.get("name") || "").trim(),
        message_template: String(form.get("message_template") || "").trim(),
        ...(editing ? {} : { type: String(form.get("type") || "broadcast") }),
        ...(schedule === "" ? {} : { schedule_at: schedule }),
        ...(expires === "" ? {} : { expires_at: expires }),
        ...(rewardType === "" ? {} : {
          reward_type: rewardType,
          reward_value: rewardValue === "" ? 0 : Number(rewardValue),
        }),
        ...(editing ? { is_active: form.get("is_active") === "on" } : {}),
      },
    });
  }

  return (
    <div className="mx-auto max-w-5xl">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">行銷活動</h1>
        <div className="flex items-center gap-3 text-sm">
          <a href="/notifications" className="text-muted hover:text-ink">發送紀錄 →</a>
          <button
            onClick={() => { setEditing(null); setMessage(null); document.getElementById("cmp-form")?.scrollIntoView(); }}
            className="rounded-lg bg-brand px-4 py-2 font-semibold text-white hover:bg-brand-deep"
          >
            新增活動
          </button>
        </div>
      </header>

      {message && (
        <p className={`mt-3 rounded-lg px-3 py-2 text-sm ${
          message.kind === "ok" ? "bg-ok-soft text-ok" : "bg-danger-soft text-danger"
        }`}>{message.text}</p>
      )}

      <div className="mt-4 overflow-x-auto rounded-xl border border-line bg-surface">
        <table className="w-full min-w-[640px] text-sm">
          <thead>
            <tr className="border-b border-line text-left text-muted">
              <th className="px-4 py-2.5 font-medium">名稱</th>
              <th className="px-4 py-2.5 font-medium">類型</th>
              <th className="px-4 py-2.5 font-medium">排程</th>
              <th className="px-4 py-2.5 font-medium">回饋</th>
              <th className="px-4 py-2.5 font-medium">狀態</th>
              <th className="px-4 py-2.5 font-medium"></th>
            </tr>
          </thead>
          <tbody>
            {campaigns.isLoading && (
              <tr><td colSpan={6} className="px-4 py-8 text-center text-muted">載入中…</td></tr>
            )}
            {campaigns.data?.length === 0 && (
              <tr><td colSpan={6} className="px-4 py-8 text-center text-muted">尚無行銷活動。</td></tr>
            )}
            {campaigns.data?.map((c) => (
              <tr key={c.id} className="border-b border-line/60">
                <td className="px-4 py-2.5 font-medium">{c.name}</td>
                <td className="px-4 py-2.5">{TYPE_LABELS[c.type] ?? c.type}</td>
                <td className="px-4 py-2.5 text-xs">
                  {c.schedule_at ? c.schedule_at.slice(0, 16).replace("T", " ") : "手動"}
                </td>
                <td className="px-4 py-2.5 text-xs">
                  {c.reward_type === "coupon" && "優惠券"}
                  {c.reward_type === "points" && `點數 ${c.reward_value ?? 0}`}
                  {!c.reward_type && "—"}
                </td>
                <td className="px-4 py-2.5">
                  <span className={`rounded-full px-2 py-0.5 text-xs ${
                    c.is_active ? "bg-ok-soft text-ok" : "bg-line text-muted"
                  }`}>{c.is_active ? c.status : "停用"}</span>
                </td>
                <td className="px-4 py-2.5">
                  <div className="flex justify-end gap-1.5">
                    <button
                      onClick={() => {
                        if (window.confirm(`立即執行「${c.name}」?符合條件的顧客會收到 LINE 推播。`)) {
                          runMut.mutate(c.id);
                        }
                      }}
                      disabled={runMut.isPending}
                      className="rounded-md border border-brand px-2 py-1 text-xs text-brand hover:bg-brand-soft disabled:opacity-60">
                      立即執行
                    </button>
                    <button onClick={() => { setEditing(c); setMessage(null); }}
                      className="rounded-md border border-line px-2 py-1 text-xs hover:bg-brand-soft">
                      編輯
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <section id="cmp-form" className="mt-6 rounded-xl border border-line bg-surface p-6">
        <h2 className="font-semibold">{editing ? `編輯:${editing.name}` : "新增活動"}</h2>
        <form key={editing?.id ?? "new"} className="mt-4 grid gap-3 text-sm sm:grid-cols-2" onSubmit={submit}>
          <label className="grid gap-1">
            名稱 *
            <input name="name" required maxLength={128} defaultValue={editing?.name ?? ""}
              className="rounded-lg border border-line px-3 py-2" />
          </label>
          {!editing && (
            <label className="grid gap-1">
              類型
              <select name="type" defaultValue="broadcast" className="rounded-lg border border-line px-3 py-2">
                {Object.entries(TYPE_LABELS).map(([v, l]) => (
                  <option key={v} value={v}>{l}</option>
                ))}
              </select>
            </label>
          )}
          <label className="grid gap-1 sm:col-span-2">
            訊息內容 *(可用 {"{name}"} 帶入顧客稱呼)
            <textarea name="message_template" required rows={3}
              defaultValue={editing?.message_template ?? ""}
              className="rounded-lg border border-line px-3 py-2" />
          </label>
          <label className="grid gap-1">
            排程時間(留空=手動執行)
            <input name="schedule_at" type="datetime-local"
              defaultValue={editing?.schedule_at ? editing.schedule_at.slice(0, 16) : ""}
              className="rounded-lg border border-line px-3 py-2" />
          </label>
          <label className="grid gap-1">
            截止時間(選填)
            <input name="expires_at" type="datetime-local"
              defaultValue={editing?.expires_at ? editing.expires_at.slice(0, 16) : ""}
              className="rounded-lg border border-line px-3 py-2" />
          </label>
          <label className="grid gap-1">
            回饋類型
            <select name="reward_type" defaultValue={editing?.reward_type ?? ""}
              className="rounded-lg border border-line px-3 py-2">
              <option value="">無</option>
              <option value="coupon">優惠券</option>
              <option value="points">點數</option>
            </select>
          </label>
          <label className="grid gap-1">
            回饋值
            <input name="reward_value" type="number" min={0}
              defaultValue={editing?.reward_value ?? ""}
              className="rounded-lg border border-line px-3 py-2" />
          </label>
          {editing && (
            <label className="flex items-center gap-2 sm:col-span-2">
              <input name="is_active" type="checkbox" defaultChecked={editing.is_active} />
              啟用中
            </label>
          )}
          <div className="flex gap-2 sm:col-span-2">
            <button disabled={saveMut.isPending}
              className="rounded-lg bg-brand px-4 py-2 font-semibold text-white hover:bg-brand-deep disabled:opacity-60">
              {saveMut.isPending ? "儲存中…" : editing ? "儲存變更" : "建立活動"}
            </button>
            {editing && (
              <button type="button" onClick={() => setEditing(null)}
                className="rounded-lg border border-line px-4 py-2">
                取消編輯
              </button>
            )}
          </div>
        </form>
      </section>
    </div>
  );
}
