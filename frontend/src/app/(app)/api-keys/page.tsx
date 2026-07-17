"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, delJson, fetchJson, postJson } from "@/lib/client-api";
import { DataTable, type Column } from "@/components/ui/DataTable";

type ApiKeyItem = { id: number; name: string; key_prefix: string; is_active: boolean; created_at: string };
type ApiKeyCreated = ApiKeyItem & { plain_key: string };

function errText(e: unknown): string {
  return e instanceof ApiError ? e.detail || `錯誤(${e.status})` : "操作失敗,請重試。";
}

export default function ApiKeysPage() {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [created, setCreated] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  const keys = useQuery({ queryKey: ["api-keys"], queryFn: () => fetchJson<ApiKeyItem[]>("/api-keys/") });

  const add = useMutation({
    mutationFn: () => postJson<ApiKeyCreated>("/api-keys/", { name: name.trim() }),
    onSuccess: (k) => { setName(""); setCreated(k.plain_key); setMsg(null); qc.invalidateQueries({ queryKey: ["api-keys"] }); },
    onError: (e) => setMsg({ kind: "error", text: errText(e) }),
  });
  const revoke = useMutation({
    mutationFn: (id: number) => delJson<void>(`/api-keys/${id}`),
    onSuccess: () => { setMsg({ kind: "ok", text: "金鑰已撤銷。" }); qc.invalidateQueries({ queryKey: ["api-keys"] }); },
    onError: (e) => setMsg({ kind: "error", text: errText(e) }),
  });

  const columns: Column<ApiKeyItem>[] = [
    { header: "名稱", cell: (k) => <span className="font-medium">{k.name}</span> },
    { header: "前綴", cell: (k) => <code className="text-sm">{k.key_prefix}…</code> },
    { header: "狀態", cell: (k) => (k.is_active ? "啟用中" : "已撤銷") },
    { header: "建立", className: "text-muted", cell: (k) => k.created_at.slice(0, 10) },
    {
      header: "", className: "text-right",
      cell: (k) => k.is_active ? (
        <button className="text-sm text-danger hover:underline"
          onClick={() => { if (confirm("撤銷此金鑰?使用它的整合將立即失效。")) revoke.mutate(k.id); }}>撤銷</button>
      ) : <span className="text-muted">—</span>,
    },
  ];

  return (
    <div className="mx-auto max-w-3xl">
      <h1 className="text-2xl font-semibold">API 金鑰</h1>
      <p className="mt-1 text-sm text-muted">供程式整合呼叫 API(X-API-Key)。金鑰明碼僅在建立時顯示一次,請立即保存。</p>
      {msg && <p className={`mt-4 rounded-lg px-3 py-2 text-sm ${msg.kind === "ok" ? "bg-ok-soft text-ok" : "bg-danger-soft text-danger"}`}>{msg.text}</p>}

      {created && (
        <div className="mt-4 rounded-lg border border-line bg-ok-soft p-3 text-sm">
          <p className="font-semibold text-ok">金鑰已建立(僅顯示這一次):</p>
          <code className="mt-1 block break-all">{created}</code>
        </div>
      )}

      <form className="mt-4 flex gap-2" onSubmit={(e) => { e.preventDefault(); if (name.trim()) add.mutate(); }}>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="金鑰名稱(如:自動化腳本)" maxLength={128}
          className="w-64 rounded-lg border border-line bg-surface px-3 py-2 text-sm" />
        <button disabled={add.isPending} className="rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-white hover:bg-brand-deep disabled:opacity-60">建立金鑰</button>
      </form>

      <div className="mt-4">
        <DataTable columns={columns} rows={keys.data} rowKey={(k) => k.id} isLoading={keys.isLoading} emptyText="尚無 API 金鑰。" />
      </div>
    </div>
  );
}
