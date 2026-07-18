"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, delJson, fetchJson, patchJson, postJson } from "@/lib/client-api";

type TypeRow = { id: number; name: string; description: string | null; is_active: boolean };
type WindowRow = { id: number; weekday: number; start_time: string; end_time: string };
type BlockRow = { id: number; starts_at: string; ends_at: string; reason: string | null };
type ResourceRow = {
  id: number;
  resource_type_id: number;
  location_id: number | null;
  name: string;
  description: string | null;
  internal_code: string | null;
  capacity: number;
  is_active: boolean;
  available_from: string | null;
  available_until: string | null;
  windows: WindowRow[];
  blocks: BlockRow[];
};
type RequirementRow = {
  id: number;
  service_id: number;
  service_name: string;
  resource_type_id: number;
  type_name: string;
  quantity: number;
};
type NameRow = { id: number; name: string };
type Overview = {
  types: TypeRow[];
  resources: ResourceRow[];
  requirements: RequirementRow[];
  services: NameRow[];
  locations: NameRow[];
};

const WEEKDAYS = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"];

function errText(e: unknown): string {
  return e instanceof ApiError ? e.detail || `錯誤(${e.status})` : "操作失敗,請重試。";
}

function FeatureLockedCard() {
  return (
    <div className="rounded-xl border border-line bg-warn-soft p-6 text-sm">
      <p className="font-semibold text-warn">此功能未啟用</p>
      <p className="mt-2 text-ink">
        房間／設備資源屬進階功能,請至
        <a href="/ui/plan" className="mx-1 text-brand underline">方案頁</a>
        升級或啟用後再試。
      </p>
    </div>
  );
}

const inputCls = "mt-1 w-full rounded-lg border border-line bg-surface px-3 py-1.5";
const btnCls =
  "rounded-lg bg-brand px-3 py-1.5 text-sm font-semibold text-white hover:bg-brand-deep disabled:opacity-60";

export default function ResourcesPage() {
  const qc = useQueryClient();
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [editingResource, setEditingResource] = useState<ResourceRow | null>(null);

  const overview = useQuery({
    queryKey: ["resources-overview"],
    queryFn: () => fetchJson<Overview>("/resources/overview"),
    retry: false,
  });

  const refresh = () => qc.invalidateQueries({ queryKey: ["resources-overview"] });
  const onOk = () => { setMsg(null); refresh(); };
  const onErr = (e: unknown) => setMsg({ kind: "error", text: errText(e) });

  const createType = useMutation({
    mutationFn: (body: Record<string, unknown>) => postJson("/resources/types", body),
    onSuccess: onOk, onError: onErr,
  });
  const toggleType = useMutation({
    mutationFn: (input: { id: number; active: boolean }) =>
      postJson(`/resources/types/${input.id}/active`, { active: input.active }),
    onSuccess: onOk, onError: onErr,
  });
  const createResource = useMutation({
    mutationFn: (body: Record<string, unknown>) => postJson("/resources", body),
    onSuccess: onOk, onError: onErr,
  });
  const updateResource = useMutation({
    mutationFn: (input: { id: number; body: Record<string, unknown> }) =>
      patchJson(`/resources/${input.id}`, input.body),
    onSuccess: () => { setEditingResource(null); onOk(); },
    onError: onErr,
  });
  const toggleResource = useMutation({
    mutationFn: (input: { id: number; active: boolean }) =>
      postJson(`/resources/${input.id}/active`, { active: input.active }),
    onSuccess: onOk, onError: onErr,
  });
  const setRequirement = useMutation({
    mutationFn: (body: Record<string, unknown>) => postJson("/resources/requirements", body),
    onSuccess: onOk, onError: onErr,
  });
  const deleteRequirement = useMutation({
    mutationFn: (id: number) => delJson(`/resources/requirements/${id}`),
    onSuccess: onOk, onError: onErr,
  });
  const addWindow = useMutation({
    mutationFn: (input: { id: number; body: Record<string, unknown> }) =>
      postJson(`/resources/${input.id}/availability`, input.body),
    onSuccess: onOk, onError: onErr,
  });
  const deleteWindow = useMutation({
    mutationFn: (id: number) => delJson(`/resources/availability/${id}`),
    onSuccess: onOk, onError: onErr,
  });
  const addBlock = useMutation({
    mutationFn: (input: { id: number; body: Record<string, unknown> }) =>
      postJson(`/resources/${input.id}/blocks`, input.body),
    onSuccess: onOk, onError: onErr,
  });
  const deleteBlock = useMutation({
    mutationFn: (id: number) => delJson(`/resources/blocks/${id}`),
    onSuccess: onOk, onError: onErr,
  });

  if (overview.error instanceof ApiError && overview.error.status === 403) {
    return (
      <div className="mx-auto max-w-5xl">
        <h1 className="text-2xl font-semibold">房間／設備</h1>
        <div className="mt-6"><FeatureLockedCard /></div>
      </div>
    );
  }

  const data = overview.data;
  const typeName = (id: number) => data?.types.find((t) => t.id === id)?.name ?? `#${id}`;
  const locationName = (id: number | null) =>
    id === null ? null : data?.locations.find((l) => l.id === id)?.name ?? `#${id}`;

  return (
    <div className="mx-auto max-w-5xl">
      <h1 className="text-2xl font-semibold">房間／設備</h1>
      <p className="mt-1 text-sm text-muted">
        定義資源類型與項目,設定服務需求後,預約引擎會自動檢查並分配可用資源。
      </p>
      {msg && (
        <p className={`mt-4 rounded-lg px-3 py-2 text-sm ${msg.kind === "ok" ? "bg-ok-soft text-ok" : "bg-danger-soft text-danger"}`}>
          {msg.text}
        </p>
      )}
      {overview.isLoading && <p className="mt-6 text-sm text-muted">載入中…</p>}

      {data && (
        <>
          {/* ── 資源類型 ── */}
          <section className="mt-6 rounded-xl border border-line bg-surface p-4">
            <h2 className="font-semibold">資源類型</h2>
            <p className="mt-0.5 text-xs text-muted">例:美容室、美甲桌、雷射儀。服務需求綁在類型上。</p>
            <ul className="mt-3 grid gap-1 text-sm">
              {data.types.length === 0 && <li className="text-muted">尚無類型。</li>}
              {data.types.map((t) => (
                <li key={t.id} className="flex flex-wrap items-center gap-2">
                  <span className={t.is_active ? "" : "text-muted line-through"}>{t.name}</span>
                  {t.description && <span className="text-xs text-muted">{t.description}</span>}
                  <button className="text-xs text-brand hover:underline"
                    onClick={() => toggleType.mutate({ id: t.id, active: !t.is_active })}>
                    {t.is_active ? "停用" : "啟用"}
                  </button>
                </li>
              ))}
            </ul>
            <form className="mt-3 flex flex-wrap items-end gap-2 text-sm"
              onSubmit={(e) => {
                e.preventDefault();
                const f = new FormData(e.currentTarget);
                createType.mutate({ name: String(f.get("name") ?? ""), description: String(f.get("description") ?? "") });
                e.currentTarget.reset();
              }}>
              <label>名稱<input name="name" required maxLength={128} className={`${inputCls} w-40`} /></label>
              <label>說明(選填)<input name="description" maxLength={2000} className={`${inputCls} w-56`} /></label>
              <button disabled={createType.isPending} className={btnCls}>新增類型</button>
            </form>
          </section>

          {/* ── 服務需求 ── */}
          <section className="mt-4 rounded-xl border border-line bg-surface p-4">
            <h2 className="font-semibold">服務資源需求</h2>
            <p className="mt-0.5 text-xs text-muted">某服務每次預約需要哪些類型的資源;數量不足的時段自動不可約。</p>
            <ul className="mt-3 grid gap-1 text-sm">
              {data.requirements.length === 0 && <li className="text-muted">尚無需求設定。</li>}
              {data.requirements.map((r) => (
                <li key={r.id} className="flex flex-wrap items-center gap-2">
                  <span>{r.service_name}</span>
                  <span className="text-xs text-muted">需要 {r.type_name} × {r.quantity}</span>
                  <button className="text-xs text-danger hover:underline"
                    onClick={() => deleteRequirement.mutate(r.id)}>移除</button>
                </li>
              ))}
            </ul>
            <form className="mt-3 flex flex-wrap items-end gap-2 text-sm"
              onSubmit={(e) => {
                e.preventDefault();
                const f = new FormData(e.currentTarget);
                setRequirement.mutate({
                  service_id: Number(f.get("service_id")),
                  resource_type_id: Number(f.get("resource_type_id")),
                  quantity: Number(f.get("quantity")),
                });
              }}>
              <label>服務
                <select name="service_id" required className={inputCls}>
                  {data.services.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                </select>
              </label>
              <label>資源類型
                <select name="resource_type_id" required className={inputCls}>
                  {data.types.filter((t) => t.is_active).map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
                </select>
              </label>
              <label>數量<input name="quantity" type="number" min={1} max={20} defaultValue={1} required className={`${inputCls} w-20`} /></label>
              <button disabled={setRequirement.isPending} className={btnCls}>設定需求</button>
            </form>
          </section>

          {/* ── 新增資源 ── */}
          <section className="mt-4 rounded-xl border border-line bg-surface p-4">
            <h2 className="font-semibold">{editingResource ? `編輯資源:${editingResource.name}` : "新增資源項目"}</h2>
            <form key={editingResource?.id ?? "new"} className="mt-3 grid gap-3 text-sm sm:grid-cols-3"
              onSubmit={(e) => {
                e.preventDefault();
                const f = new FormData(e.currentTarget);
                const locationId = String(f.get("location_id") ?? "").trim();
                const body = {
                  name: String(f.get("name") ?? ""),
                  description: String(f.get("description") ?? ""),
                  internal_code: String(f.get("internal_code") ?? ""),
                  capacity: Number(f.get("capacity")),
                  location_id: locationId ? Number(locationId) : null,
                  available_from: String(f.get("available_from") ?? "") || null,
                  available_until: String(f.get("available_until") ?? "") || null,
                };
                if (editingResource) {
                  updateResource.mutate({ id: editingResource.id, body });
                } else {
                  createResource.mutate({ ...body, resource_type_id: Number(f.get("resource_type_id")) });
                  e.currentTarget.reset();
                }
              }}>
              {!editingResource && (
                <label>類型
                  <select name="resource_type_id" required className={inputCls}>
                    {data.types.filter((t) => t.is_active).map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
                  </select>
                </label>
              )}
              <label>名稱<input name="name" required maxLength={128} defaultValue={editingResource?.name ?? ""} className={inputCls} /></label>
              <label>內部編號(選填)<input name="internal_code" maxLength={64} defaultValue={editingResource?.internal_code ?? ""} className={inputCls} /></label>
              <label>同時可服務數(1～100)<input name="capacity" type="number" min={1} max={100} defaultValue={editingResource?.capacity ?? 1} required className={inputCls} /></label>
              {data.locations.length > 0 && (
                <label>分店(選填)
                  <select name="location_id" defaultValue={editingResource?.location_id ?? ""} className={inputCls}>
                    <option value="">不限</option>
                    {data.locations.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}
                  </select>
                </label>
              )}
              <label>可用起日(選填)<input name="available_from" type="date" defaultValue={editingResource?.available_from ?? ""} className={inputCls} /></label>
              <label>可用迄日(選填)<input name="available_until" type="date" defaultValue={editingResource?.available_until ?? ""} className={inputCls} /></label>
              <label className="sm:col-span-3">說明(選填)<input name="description" maxLength={2000} defaultValue={editingResource?.description ?? ""} className={inputCls} /></label>
              <div className="flex gap-2 sm:col-span-3">
                <button disabled={createResource.isPending || updateResource.isPending} className={btnCls}>
                  {editingResource ? "儲存" : "新增資源"}
                </button>
                {editingResource && (
                  <button type="button" className="rounded-lg border border-line px-3 py-1.5 text-sm hover:bg-line/20"
                    onClick={() => setEditingResource(null)}>取消</button>
                )}
              </div>
            </form>
          </section>

          {/* ── 資源清單 ── */}
          <div className="mt-4 grid gap-4">
            {data.resources.length === 0 && (
              <p className="rounded-xl border border-line bg-surface p-6 text-sm text-muted">尚無資源項目。</p>
            )}
            {data.resources.map((r) => (
              <div key={r.id} className="rounded-xl border border-line bg-surface p-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <h3 className="font-semibold">
                      {r.name}
                      <span className="ml-2 text-xs font-normal text-muted">{typeName(r.resource_type_id)}</span>
                      <span className={`ml-2 rounded-full px-2 py-0.5 text-xs ${r.is_active ? "bg-ok-soft text-ok" : "bg-line/40 text-muted"}`}>
                        {r.is_active ? "啟用中" : "已停用"}
                      </span>
                    </h3>
                    <p className="mt-0.5 text-xs text-muted">
                      容量 {r.capacity}
                      {r.internal_code ? `・編號 ${r.internal_code}` : ""}
                      {locationName(r.location_id) ? `・${locationName(r.location_id)}` : ""}
                      {r.available_from || r.available_until
                        ? `・可用 ${r.available_from ?? "…"}～${r.available_until ?? "…"}`
                        : ""}
                    </p>
                  </div>
                  <div className="flex gap-2 text-sm">
                    <button className="text-brand hover:underline" onClick={() => setEditingResource(r)}>編輯</button>
                    <button className={r.is_active ? "text-danger hover:underline" : "text-ok hover:underline"}
                      onClick={() => toggleResource.mutate({ id: r.id, active: !r.is_active })}>
                      {r.is_active ? "停用" : "啟用"}
                    </button>
                  </div>
                </div>

                <div className="mt-3 grid gap-4 sm:grid-cols-2">
                  <div>
                    <h4 className="text-sm font-medium">每週可用時段</h4>
                    <p className="text-xs text-muted">未設定 = 全時段可用。</p>
                    <ul className="mt-1 grid gap-1 text-sm">
                      {r.windows.map((w) => (
                        <li key={w.id} className="flex items-center gap-2">
                          <span>{WEEKDAYS[w.weekday]} {w.start_time.slice(0, 5)}–{w.end_time.slice(0, 5)}</span>
                          <button className="text-xs text-danger hover:underline"
                            onClick={() => deleteWindow.mutate(w.id)}>刪除</button>
                        </li>
                      ))}
                    </ul>
                    <form className="mt-2 flex flex-wrap items-end gap-2 text-xs"
                      onSubmit={(e) => {
                        e.preventDefault();
                        const f = new FormData(e.currentTarget);
                        addWindow.mutate({
                          id: r.id,
                          body: {
                            weekday: Number(f.get("weekday")),
                            start_time: String(f.get("start_time")),
                            end_time: String(f.get("end_time")),
                          },
                        });
                        e.currentTarget.reset();
                      }}>
                      <select name="weekday" className="rounded-lg border border-line bg-surface px-2 py-1">
                        {WEEKDAYS.map((d, i) => <option key={i} value={i}>{d}</option>)}
                      </select>
                      <input name="start_time" type="time" required className="rounded-lg border border-line bg-surface px-2 py-1" />
                      <input name="end_time" type="time" required className="rounded-lg border border-line bg-surface px-2 py-1" />
                      <button className="rounded-lg bg-brand px-2 py-1 font-semibold text-white hover:bg-brand-deep">加入</button>
                    </form>
                  </div>

                  <div>
                    <h4 className="text-sm font-medium">停用時段(維修/保養)</h4>
                    <ul className="mt-1 grid gap-1 text-sm">
                      {r.blocks.length === 0 && <li className="text-xs text-muted">無。</li>}
                      {r.blocks.map((b) => (
                        <li key={b.id} className="flex items-center gap-2">
                          <span className="text-xs">
                            {b.starts_at.slice(0, 16).replace("T", " ")} ～ {b.ends_at.slice(0, 16).replace("T", " ")}
                            {b.reason ? `(${b.reason})` : ""}
                          </span>
                          <button className="text-xs text-danger hover:underline"
                            onClick={() => deleteBlock.mutate(b.id)}>刪除</button>
                        </li>
                      ))}
                    </ul>
                    <form className="mt-2 flex flex-wrap items-end gap-2 text-xs"
                      onSubmit={(e) => {
                        e.preventDefault();
                        const f = new FormData(e.currentTarget);
                        addBlock.mutate({
                          id: r.id,
                          body: {
                            starts_at: String(f.get("starts_at")),
                            ends_at: String(f.get("ends_at")),
                            reason: String(f.get("reason") ?? ""),
                          },
                        });
                        e.currentTarget.reset();
                      }}>
                      <input name="starts_at" type="datetime-local" required className="rounded-lg border border-line bg-surface px-2 py-1" />
                      <input name="ends_at" type="datetime-local" required className="rounded-lg border border-line bg-surface px-2 py-1" />
                      <input name="reason" placeholder="原因(選填)" maxLength={255} className="w-28 rounded-lg border border-line bg-surface px-2 py-1" />
                      <button className="rounded-lg bg-brand px-2 py-1 font-semibold text-white hover:bg-brand-deep">加入</button>
                    </form>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
