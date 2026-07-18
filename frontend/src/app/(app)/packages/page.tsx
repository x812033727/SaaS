"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, fetchJson, postJson } from "@/lib/client-api";

type PackageItem = { service_id: number; service_name: string; included_quantity: number };

type PackageRow = {
  id: number;
  name: string;
  description: string | null;
  price_cents: number;
  validity_days: number;
  is_active: boolean;
  items: PackageItem[];
};

type ServiceRow = { id: number; name: string };

function errText(e: unknown): string {
  return e instanceof ApiError ? e.detail || `錯誤(${e.status})` : "操作失敗,請重試。";
}

function twd(cents: number): string {
  return `NT$${(cents / 100).toLocaleString()}`;
}

function FeatureLockedCard() {
  return (
    <div className="rounded-xl border border-line bg-warn-soft p-6 text-sm">
      <p className="font-semibold text-warn">此功能未啟用</p>
      <p className="mt-2 text-ink">
        服務套票屬進階功能,請至
        <a href="/ui/plan" className="mx-1 text-brand underline">方案頁</a>
        升級或啟用後再試。
      </p>
    </div>
  );
}

function AddItemForm({
  packageId,
  services,
  onError,
}: {
  packageId: number;
  services: ServiceRow[] | undefined;
  onError: (text: string) => void;
}) {
  const qc = useQueryClient();

  const add = useMutation({
    mutationFn: (body: Record<string, unknown>) => postJson(`/api/v1/packages/${packageId}/items`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["packages"] }),
    onError: (e) => onError(errText(e)),
  });

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    add.mutate({
      service_id: Number(form.get("service_id")),
      included_quantity: Number(form.get("included_quantity")),
    });
    event.currentTarget.reset();
  }

  return (
    <form className="mt-3 flex flex-wrap items-end gap-2 text-sm" onSubmit={submit}>
      <label>
        服務
        <select name="service_id" required
          className="mt-1 block rounded-lg border border-line bg-surface px-3 py-1.5">
          {services?.map((s) => (
            <option key={s.id} value={s.id}>{s.name}</option>
          ))}
        </select>
      </label>
      <label>
        次數(1～999;同服務重填=更新)
        <input name="included_quantity" type="number" min={1} max={999} required defaultValue={1}
          className="mt-1 block w-28 rounded-lg border border-line bg-surface px-3 py-1.5" />
      </label>
      <button disabled={add.isPending}
        className="rounded-lg bg-brand px-3 py-1.5 font-semibold text-white hover:bg-brand-deep disabled:opacity-60">
        加入服務
      </button>
    </form>
  );
}

export default function PackagesPage() {
  const qc = useQueryClient();
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  const packages = useQuery({
    queryKey: ["packages"],
    queryFn: () => fetchJson<PackageRow[]>("/api/v1/packages"),
    retry: false,
  });
  const services = useQuery({
    queryKey: ["services-for-packages"],
    queryFn: () => fetchJson<ServiceRow[]>("/booking/services/"),
    retry: false,
  });

  const create = useMutation({
    mutationFn: (body: Record<string, unknown>) => postJson("/api/v1/packages", body),
    onSuccess: () => {
      setMsg({ kind: "ok", text: "套票已建立,請加入服務組成。" });
      qc.invalidateQueries({ queryKey: ["packages"] });
    },
    onError: (e) => setMsg({ kind: "error", text: errText(e) }),
  });

  const setActive = useMutation({
    mutationFn: (input: { id: number; active: boolean }) =>
      postJson(`/api/v1/packages/${input.id}/active`, { active: input.active }),
    onSuccess: () => {
      setMsg(null);
      qc.invalidateQueries({ queryKey: ["packages"] });
    },
    onError: (e) => setMsg({ kind: "error", text: errText(e) }),
  });

  if (packages.error instanceof ApiError && packages.error.status === 403) {
    return (
      <div className="mx-auto max-w-4xl">
        <h1 className="text-2xl font-semibold">服務套票</h1>
        <div className="mt-6"><FeatureLockedCard /></div>
      </div>
    );
  }

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    create.mutate({
      name: String(form.get("name") ?? ""),
      description: String(form.get("description") ?? ""),
      price_twd: Number(form.get("price_twd")),
      validity_days: Number(form.get("validity_days")),
    });
    event.currentTarget.reset();
  }

  return (
    <div className="mx-auto max-w-4xl">
      <h1 className="text-2xl font-semibold">服務套票</h1>
      <p className="mt-1 text-sm text-muted">
        定義套票(多次服務優惠組合);顧客購買後於顧客頁發放,預約自動折抵次數。
      </p>
      {msg && (
        <p className={`mt-4 rounded-lg px-3 py-2 text-sm ${msg.kind === "ok" ? "bg-ok-soft text-ok" : "bg-danger-soft text-danger"}`}>
          {msg.text}
        </p>
      )}

      <form className="mt-6 grid gap-3 rounded-xl border border-line bg-surface p-4" onSubmit={submit}>
        <h2 className="font-semibold">新增套票</h2>
        <div className="grid gap-3 sm:grid-cols-3">
          <label className="text-sm">
            名稱
            <input name="name" required maxLength={128}
              className="mt-1 w-full rounded-lg border border-line bg-surface px-3 py-2" />
          </label>
          <label className="text-sm">
            售價(NT$)
            <input name="price_twd" type="number" min={0} max={20000000} required
              className="mt-1 w-full rounded-lg border border-line bg-surface px-3 py-2" />
          </label>
          <label className="text-sm">
            有效天數(1～3650)
            <input name="validity_days" type="number" min={1} max={3650} required defaultValue={365}
              className="mt-1 w-full rounded-lg border border-line bg-surface px-3 py-2" />
          </label>
        </div>
        <label className="text-sm">
          說明(選填)
          <textarea name="description" maxLength={2000} rows={2}
            className="mt-1 w-full rounded-lg border border-line bg-surface px-3 py-2" />
        </label>
        <div>
          <button disabled={create.isPending}
            className="rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-white hover:bg-brand-deep disabled:opacity-60">
            建立套票
          </button>
        </div>
      </form>

      <div className="mt-6 grid gap-4">
        {packages.isLoading && <p className="text-sm text-muted">載入中…</p>}
        {packages.data?.length === 0 && (
          <p className="rounded-xl border border-line bg-surface p-6 text-sm text-muted">尚無套票。</p>
        )}
        {packages.data?.map((p) => (
          <div key={p.id} className="rounded-xl border border-line bg-surface p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <h3 className="font-semibold">
                  {p.name}
                  <span className={`ml-2 rounded-full px-2 py-0.5 text-xs ${p.is_active ? "bg-ok-soft text-ok" : "bg-line/40 text-muted"}`}>
                    {p.is_active ? "販售中" : "已停售"}
                  </span>
                </h3>
                <p className="mt-0.5 text-xs text-muted">
                  {twd(p.price_cents)}・效期 {p.validity_days} 天
                  {p.description ? `・${p.description}` : ""}
                </p>
              </div>
              <button
                className={`rounded-lg px-3 py-1.5 text-sm font-semibold ${p.is_active ? "border border-line hover:bg-line/20" : "bg-brand text-white hover:bg-brand-deep"}`}
                onClick={() => setActive.mutate({ id: p.id, active: !p.is_active })}
              >
                {p.is_active ? "停售" : "恢復販售"}
              </button>
            </div>
            {p.items.length > 0 ? (
              <ul className="mt-3 grid gap-1 text-sm">
                {p.items.map((i) => (
                  <li key={i.service_id} className="flex items-baseline gap-2">
                    <span>{i.service_name}</span>
                    <span className="text-xs text-muted">× {i.included_quantity} 次</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-3 text-sm text-muted">尚未加入服務;至少一項服務才適合販售。</p>
            )}
            <AddItemForm packageId={p.id} services={services.data}
              onError={(text) => setMsg({ kind: "error", text })} />
          </div>
        ))}
      </div>
    </div>
  );
}
