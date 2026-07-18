"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, fetchJson, postJson } from "@/lib/client-api";

type StaffRow = {
  id: number;
  name: string;
  role: string | null;
  location_id: number | null;
  booking_mode: string;
  is_active: boolean;
  access_token: string | null;
};

type ShiftRow = {
  id: number;
  staff_id: number;
  weekday: number | null;
  start_time: string;
  end_time: string;
  rotation: string | null;
  is_active: boolean;
};

type LeaveRow = {
  id: number;
  staff_id: number;
  start_at: string;
  end_at: string;
  reason: string | null;
  status: string;
};

type LocationRow = { id: number; name: string };

const WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"];

function errText(error: unknown): string {
  if (error instanceof ApiError) return error.detail || `錯誤(${error.status})`;
  return "操作失敗,請重試。";
}

function weekdayLabel(w: number | null): string {
  return w === null ? "每日" : `週${WEEKDAYS[w]}`;
}

function FeatureLockedCard() {
  return (
    <div className="rounded-xl border border-line bg-warn-soft p-6 text-sm">
      <p className="font-semibold text-warn">此功能未啟用</p>
      <p className="mt-2 text-ink">
        員工排班屬進階功能,請至
        <a href="/console/plan" className="mx-1 text-brand underline">方案頁</a>
        升級或啟用後再試。
      </p>
    </div>
  );
}

/** 單一員工的班表+請假展開面板。 */
function StaffDetail({ staff, onMessage }: {
  staff: StaffRow;
  onMessage: (m: { kind: "ok" | "error"; text: string }) => void;
}) {
  const qc = useQueryClient();
  const shifts = useQuery({
    queryKey: ["staff-shifts", staff.id],
    queryFn: () => fetchJson<ShiftRow[]>(`/booking/staff/${staff.id}/shifts`),
    retry: false,
  });
  const leaves = useQuery({
    queryKey: ["staff-leaves", staff.id],
    queryFn: () => fetchJson<LeaveRow[]>(`/booking/staff/${staff.id}/leaves`),
    retry: false,
  });
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["staff-shifts", staff.id] });
    qc.invalidateQueries({ queryKey: ["staff-leaves", staff.id] });
  };

  const addShift = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      postJson(`/booking/staff/${staff.id}/shifts`, body),
    onSuccess: () => { invalidate(); onMessage({ kind: "ok", text: "班表已新增。" }); },
    onError: (e) => onMessage({ kind: "error", text: errText(e) }),
  });
  const delShift = useMutation({
    mutationFn: (shiftId: number) =>
      fetchJson(`/booking/staff/${staff.id}/shifts/${shiftId}`, { method: "DELETE" }),
    onSuccess: () => { invalidate(); onMessage({ kind: "ok", text: "班表已刪除。" }); },
    onError: (e) => onMessage({ kind: "error", text: errText(e) }),
  });
  const addLeave = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      postJson(`/booking/staff/${staff.id}/leaves`, body),
    onSuccess: () => { invalidate(); onMessage({ kind: "ok", text: "請假已登記。" }); },
    onError: (e) => onMessage({ kind: "error", text: errText(e) }),
  });
  const delLeave = useMutation({
    mutationFn: (leaveId: number) =>
      fetchJson(`/booking/staff/${staff.id}/leaves/${leaveId}`, { method: "DELETE" }),
    onSuccess: () => { invalidate(); onMessage({ kind: "ok", text: "請假已刪除。" }); },
    onError: (e) => onMessage({ kind: "error", text: errText(e) }),
  });

  function submitShift(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const weekday = String(form.get("weekday") ?? "");
    addShift.mutate({
      start_time: String(form.get("start_time") || ""),
      end_time: String(form.get("end_time") || ""),
      ...(weekday === "" ? {} : { weekday: Number(weekday) }),
    });
    event.currentTarget.reset();
  }

  function submitLeave(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    addLeave.mutate({
      start_at: String(form.get("start_at") || ""),
      end_at: String(form.get("end_at") || ""),
      reason: String(form.get("reason") || "").trim() || null,
    });
    event.currentTarget.reset();
  }

  return (
    <div className="grid gap-4 border-t border-line/60 bg-bg/40 px-4 py-4 text-sm lg:grid-cols-2">
      <section>
        <h3 className="font-semibold">每週班表</h3>
        <ul className="mt-2 grid gap-1.5">
          {shifts.data?.length === 0 && <li className="text-muted">尚未設定班表。</li>}
          {shifts.data?.map((s) => (
            <li key={s.id} className="flex items-center justify-between rounded-lg border border-line bg-surface px-3 py-1.5">
              <span>
                {weekdayLabel(s.weekday)} {s.start_time}–{s.end_time}
                {s.rotation ? `(${s.rotation})` : ""}
                {!s.is_active && <span className="ml-1 text-xs text-muted">(停用)</span>}
              </span>
              <button onClick={() => delShift.mutate(s.id)}
                className="rounded-md border border-line px-2 py-0.5 text-xs text-danger hover:bg-danger-soft">
                刪除
              </button>
            </li>
          ))}
        </ul>
        <form className="mt-3 flex flex-wrap items-end gap-2" onSubmit={submitShift}>
          <label className="grid gap-1 text-xs">
            星期
            <select name="weekday" className="rounded-lg border border-line px-2 py-1.5">
              <option value="">每日</option>
              {WEEKDAYS.map((w, i) => <option key={i} value={i}>週{w}</option>)}
            </select>
          </label>
          <label className="grid gap-1 text-xs">
            開始
            <input name="start_time" type="time" required className="rounded-lg border border-line px-2 py-1.5" />
          </label>
          <label className="grid gap-1 text-xs">
            結束
            <input name="end_time" type="time" required className="rounded-lg border border-line px-2 py-1.5" />
          </label>
          <button disabled={addShift.isPending}
            className="rounded-lg bg-brand px-3 py-1.5 text-xs font-semibold text-white hover:bg-brand-deep disabled:opacity-60">
            加入班表
          </button>
        </form>
      </section>

      <section>
        <h3 className="font-semibold">請假</h3>
        <ul className="mt-2 grid gap-1.5">
          {leaves.data?.length === 0 && <li className="text-muted">近期無請假。</li>}
          {leaves.data?.map((l) => (
            <li key={l.id} className="flex items-center justify-between rounded-lg border border-line bg-surface px-3 py-1.5">
              <span>
                {l.start_at.slice(0, 16).replace("T", " ")} → {l.end_at.slice(0, 16).replace("T", " ")}
                {l.reason ? `:${l.reason}` : ""}
              </span>
              <button onClick={() => delLeave.mutate(l.id)}
                className="rounded-md border border-line px-2 py-0.5 text-xs text-danger hover:bg-danger-soft">
                刪除
              </button>
            </li>
          ))}
        </ul>
        <form className="mt-3 flex flex-wrap items-end gap-2" onSubmit={submitLeave}>
          <label className="grid gap-1 text-xs">
            開始
            <input name="start_at" type="datetime-local" required className="rounded-lg border border-line px-2 py-1.5" />
          </label>
          <label className="grid gap-1 text-xs">
            結束
            <input name="end_at" type="datetime-local" required className="rounded-lg border border-line px-2 py-1.5" />
          </label>
          <label className="grid gap-1 text-xs">
            事由
            <input name="reason" maxLength={255} className="rounded-lg border border-line px-2 py-1.5" />
          </label>
          <button disabled={addLeave.isPending}
            className="rounded-lg bg-brand px-3 py-1.5 text-xs font-semibold text-white hover:bg-brand-deep disabled:opacity-60">
            登記請假
          </button>
        </form>
      </section>
    </div>
  );
}

export default function StaffPage() {
  const qc = useQueryClient();
  const [editing, setEditing] = useState<StaffRow | null>(null);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  const staff = useQuery({
    queryKey: ["staff-admin"],
    queryFn: () => fetchJson<StaffRow[]>("/booking/staff/"),
    retry: false,
  });
  const locations = useQuery({
    queryKey: ["locations-admin"],
    queryFn: () => fetchJson<LocationRow[]>("/booking/locations/"),
    retry: false,
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["staff-admin"] });

  const saveMut = useMutation({
    mutationFn: async (input: { id: number | null; body: Record<string, unknown> }) =>
      input.id === null
        ? postJson("/booking/staff/", input.body)
        : fetchJson(`/booking/staff/${input.id}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(input.body),
          }),
    onSuccess: () => { invalidate(); setEditing(null); setMessage({ kind: "ok", text: "已儲存。" }); },
    onError: (e) => setMessage({ kind: "error", text: errText(e) }),
  });

  const rotateMut = useMutation({
    mutationFn: (staffId: number) =>
      postJson<StaffRow>(`/booking/staff/${staffId}/rotate-token`, {}),
    onSuccess: (updated) => {
      invalidate();
      setMessage({
        kind: "ok",
        text: `員工入口連結已輪替;新 token:${updated.access_token ?? "(未回傳)"}`,
      });
    },
    onError: (e) => setMessage({ kind: "error", text: errText(e) }),
  });

  if (staff.error instanceof ApiError && staff.error.status === 403) {
    return (
      <div className="mx-auto max-w-5xl">
        <h1 className="text-2xl font-semibold">員工</h1>
        <div className="mt-6"><FeatureLockedCard /></div>
      </div>
    );
  }

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const locationId = String(form.get("location_id") ?? "");
    saveMut.mutate({
      id: editing?.id ?? null,
      body: {
        name: String(form.get("name") || "").trim(),
        role: String(form.get("role") || "").trim() || null,
        booking_mode: String(form.get("booking_mode") || "capacity"),
        location_id: locationId === "" ? null : Number(locationId),
        ...(editing ? { is_active: form.get("is_active") === "on" } : {}),
      },
    });
  }

  const locationName = (id: number | null) =>
    id === null ? "—" : locations.data?.find((l) => l.id === id)?.name ?? `#${id}`;

  return (
    <div className="mx-auto max-w-5xl">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">員工</h1>
        <div className="flex items-center gap-3 text-sm">
          <a href="/ui/staff" className="text-muted hover:text-ink">員工入口/進階 → 舊版</a>
          <button
            onClick={() => { setEditing(null); setMessage(null); document.getElementById("staff-form")?.scrollIntoView(); }}
            className="rounded-lg bg-brand px-4 py-2 font-semibold text-white hover:bg-brand-deep"
          >
            新增員工
          </button>
        </div>
      </header>

      {message && (
        <p className={`mt-3 break-all rounded-lg px-3 py-2 text-sm ${
          message.kind === "ok" ? "bg-ok-soft text-ok" : "bg-danger-soft text-danger"
        }`}>{message.text}</p>
      )}

      <div className="mt-4 overflow-x-auto rounded-xl border border-line bg-surface">
        <table className="w-full min-w-[640px] text-sm">
          <thead>
            <tr className="border-b border-line text-left text-muted">
              <th className="px-4 py-2.5 font-medium">姓名</th>
              <th className="px-4 py-2.5 font-medium">職稱</th>
              <th className="px-4 py-2.5 font-medium">分店</th>
              <th className="px-4 py-2.5 font-medium">預約模式</th>
              <th className="px-4 py-2.5 font-medium">狀態</th>
              <th className="px-4 py-2.5 font-medium"></th>
            </tr>
          </thead>
          <tbody>
            {staff.isLoading && (
              <tr><td colSpan={6} className="px-4 py-8 text-center text-muted">載入中…</td></tr>
            )}
            {staff.data?.length === 0 && (
              <tr><td colSpan={6} className="px-4 py-8 text-center text-muted">尚無員工。</td></tr>
            )}
            {staff.data?.map((s) => (
              <StaffRows key={s.id} s={s}
                expanded={expanded === s.id}
                locationName={locationName(s.location_id)}
                onToggle={() => setExpanded(expanded === s.id ? null : s.id)}
                onEdit={() => { setEditing(s); setMessage(null); }}
                onRotate={() => rotateMut.mutate(s.id)}
                onMessage={setMessage}
              />
            ))}
          </tbody>
        </table>
      </div>

      <section id="staff-form" className="mt-6 rounded-xl border border-line bg-surface p-6">
        <h2 className="font-semibold">{editing ? `編輯:${editing.name}` : "新增員工"}</h2>
        <form key={editing?.id ?? "new"} className="mt-4 grid gap-3 text-sm sm:grid-cols-2" onSubmit={submit}>
          <label className="grid gap-1">
            姓名 *
            <input name="name" required maxLength={128} defaultValue={editing?.name ?? ""}
              className="rounded-lg border border-line px-3 py-2" />
          </label>
          <label className="grid gap-1">
            職稱
            <input name="role" maxLength={64} defaultValue={editing?.role ?? ""}
              className="rounded-lg border border-line px-3 py-2" />
          </label>
          <label className="grid gap-1">
            預約模式
            <select name="booking_mode" defaultValue={editing?.booking_mode ?? "capacity"}
              className="rounded-lg border border-line px-3 py-2">
              <option value="capacity">容量制(同時段可多組)</option>
              <option value="one_to_one">一對一(同時段限一組)</option>
            </select>
          </label>
          <label className="grid gap-1">
            所屬分店
            <select name="location_id" defaultValue={editing?.location_id ?? ""}
              className="rounded-lg border border-line px-3 py-2">
              <option value="">不指定</option>
              {locations.data?.map((l) => (
                <option key={l.id} value={l.id}>{l.name}</option>
              ))}
            </select>
          </label>
          {editing && (
            <label className="flex items-center gap-2 sm:col-span-2">
              <input name="is_active" type="checkbox" defaultChecked={editing.is_active} />
              在職中
            </label>
          )}
          <div className="flex gap-2 sm:col-span-2">
            <button disabled={saveMut.isPending}
              className="rounded-lg bg-brand px-4 py-2 font-semibold text-white hover:bg-brand-deep disabled:opacity-60">
              {saveMut.isPending ? "儲存中…" : editing ? "儲存變更" : "建立員工"}
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

function StaffRows({ s, expanded, locationName, onToggle, onEdit, onRotate, onMessage }: {
  s: StaffRow;
  expanded: boolean;
  locationName: string;
  onToggle: () => void;
  onEdit: () => void;
  onRotate: () => void;
  onMessage: (m: { kind: "ok" | "error"; text: string }) => void;
}) {
  return (
    <>
      <tr className="border-b border-line/60">
        <td className="px-4 py-2.5 font-medium">{s.name}</td>
        <td className="px-4 py-2.5">{s.role || "—"}</td>
        <td className="px-4 py-2.5">{locationName}</td>
        <td className="px-4 py-2.5">{s.booking_mode === "one_to_one" ? "一對一" : "容量制"}</td>
        <td className="px-4 py-2.5">
          <span className={`rounded-full px-2 py-0.5 text-xs ${
            s.is_active ? "bg-ok-soft text-ok" : "bg-line text-muted"
          }`}>{s.is_active ? "在職" : "停用"}</span>
        </td>
        <td className="px-4 py-2.5">
          <div className="flex justify-end gap-1.5">
            <button onClick={onToggle}
              className="rounded-md border border-line px-2 py-1 text-xs hover:bg-brand-soft">
              {expanded ? "收合" : "班表/請假"}
            </button>
            <button onClick={onEdit}
              className="rounded-md border border-line px-2 py-1 text-xs hover:bg-brand-soft">
              編輯
            </button>
            <button
              onClick={() => {
                if (window.confirm("輪替後舊的員工入口連結會立即失效,確定?")) onRotate();
              }}
              className="rounded-md border border-line px-2 py-1 text-xs hover:bg-brand-soft">
              輪替入口
            </button>
          </div>
        </td>
      </tr>
      {expanded && (
        <tr>
          <td colSpan={6} className="p-0">
            <StaffDetail staff={s} onMessage={onMessage} />
          </td>
        </tr>
      )}
    </>
  );
}
