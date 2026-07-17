"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, delJson, fetchJson, postJson } from "@/lib/client-api";
import { DataTable, type Column } from "@/components/ui/DataTable";

type Note = { id: number; title: string; content: string };

function errText(e: unknown): string {
  return e instanceof ApiError ? e.detail || `錯誤(${e.status})` : "操作失敗,請重試。";
}

export default function NotesPage() {
  const qc = useQueryClient();
  const [form, setForm] = useState({ title: "", content: "" });
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  const notes = useQuery({ queryKey: ["notes"], queryFn: () => fetchJson<Note[]>("/notes/") });

  const add = useMutation({
    mutationFn: () => postJson<Note>("/notes/", { title: form.title.trim(), content: form.content.trim() }),
    onSuccess: () => { setForm({ title: "", content: "" }); setMsg({ kind: "ok", text: "備註已新增。" }); qc.invalidateQueries({ queryKey: ["notes"] }); },
    onError: (e) => setMsg({ kind: "error", text: errText(e) }),
  });
  const del = useMutation({
    mutationFn: (id: number) => delJson<void>(`/notes/${id}`),
    onSuccess: () => { setMsg({ kind: "ok", text: "備註已刪除。" }); qc.invalidateQueries({ queryKey: ["notes"] }); },
    onError: (e) => setMsg({ kind: "error", text: errText(e) }),
  });

  const columns: Column<Note>[] = [
    { header: "標題", cell: (n) => <span className="font-medium">{n.title}</span> },
    { header: "內容", cell: (n) => <span className="text-muted">{n.content}</span> },
    {
      header: "", className: "text-right",
      cell: (n) => (
        <button className="text-sm text-danger hover:underline"
          onClick={() => { if (confirm("刪除此備註?")) del.mutate(n.id); }}>刪除</button>
      ),
    },
  ];

  return (
    <div className="mx-auto max-w-3xl">
      <h1 className="text-2xl font-semibold">備註</h1>
      {msg && <p className={`mt-4 rounded-lg px-3 py-2 text-sm ${msg.kind === "ok" ? "bg-ok-soft text-ok" : "bg-danger-soft text-danger"}`}>{msg.text}</p>}

      <form className="mt-4 grid gap-2" onSubmit={(e) => { e.preventDefault(); if (form.title.trim()) add.mutate(); }}>
        <input value={form.title} onChange={(e) => setForm((f) => ({ ...f, title: e.target.value }))}
          placeholder="標題" className="rounded-lg border border-line bg-surface px-3 py-2" />
        <textarea value={form.content} onChange={(e) => setForm((f) => ({ ...f, content: e.target.value }))}
          placeholder="內容" rows={2} className="rounded-lg border border-line bg-surface px-3 py-2" />
        <div>
          <button disabled={add.isPending} className="rounded-lg bg-brand px-4 py-2 font-semibold text-white hover:bg-brand-deep disabled:opacity-60">新增備註</button>
        </div>
      </form>

      <div className="mt-4">
        <DataTable columns={columns} rows={notes.data} rowKey={(n) => n.id} isLoading={notes.isLoading} emptyText="尚無備註。" />
      </div>
    </div>
  );
}
