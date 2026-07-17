"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, delJson, fetchJson, postJson } from "@/lib/client-api";
import { DataTable, type Column } from "@/components/ui/DataTable";

type Category = { id: number; name: string; sort_order: number; is_active: boolean };
type Item = {
  id: number;
  category_id: number | null;
  image_url: string;
  caption: string | null;
  sort_order: number;
  is_active: boolean;
};

function errText(e: unknown): string {
  if (e instanceof ApiError) return e.detail || `錯誤(${e.status})`;
  return "操作失敗,請重試。";
}

export default function PortfolioPage() {
  const qc = useQueryClient();
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [newCat, setNewCat] = useState("");
  const [itemForm, setItemForm] = useState({ image_url: "", caption: "", category_id: "" });

  const categories = useQuery({
    queryKey: ["portfolio-categories"],
    queryFn: () => fetchJson<Category[]>("/booking/portfolio/categories"),
  });
  const items = useQuery({
    queryKey: ["portfolio-items"],
    queryFn: () => fetchJson<Item[]>("/booking/portfolio/items"),
  });

  const catName = (id: number | null) =>
    categories.data?.find((c) => c.id === id)?.name ?? "—";

  const addCat = useMutation({
    mutationFn: () => postJson<Category>("/booking/portfolio/categories", { name: newCat.trim() }),
    onSuccess: () => { setNewCat(""); setMsg({ kind: "ok", text: "分類已新增。" }); qc.invalidateQueries({ queryKey: ["portfolio-categories"] }); },
    onError: (e) => setMsg({ kind: "error", text: errText(e) }),
  });

  const addItem = useMutation({
    mutationFn: () => postJson<Item>("/booking/portfolio/items", {
      image_url: itemForm.image_url.trim(),
      caption: itemForm.caption.trim() || null,
      category_id: itemForm.category_id ? Number(itemForm.category_id) : null,
    }),
    onSuccess: () => { setItemForm({ image_url: "", caption: "", category_id: "" }); setMsg({ kind: "ok", text: "作品已新增。" }); qc.invalidateQueries({ queryKey: ["portfolio-items"] }); },
    onError: (e) => setMsg({ kind: "error", text: errText(e) }),
  });

  const delItem = useMutation({
    mutationFn: (id: number) => delJson<void>(`/booking/portfolio/items/${id}`),
    onSuccess: () => { setMsg({ kind: "ok", text: "作品已刪除。" }); qc.invalidateQueries({ queryKey: ["portfolio-items"] }); },
    onError: (e) => setMsg({ kind: "error", text: errText(e) }),
  });

  const columns: Column<Item>[] = [
    {
      header: "圖片",
      cell: (it) => (
        // eslint-disable-next-line @next/next/no-img-element
        <img src={it.image_url} alt={it.caption ?? ""} className="h-12 w-12 rounded object-cover" />
      ),
    },
    { header: "說明", cell: (it) => it.caption ?? "—" },
    { header: "分類", cell: (it) => catName(it.category_id) },
    { header: "狀態", cell: (it) => (it.is_active ? "顯示中" : "隱藏") },
    {
      header: "",
      className: "text-right",
      cell: (it) => (
        <button className="text-sm text-danger hover:underline"
          onClick={() => { if (confirm("刪除此作品?")) delItem.mutate(it.id); }}>
          刪除
        </button>
      ),
    },
  ];

  return (
    <div className="mx-auto max-w-4xl">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">作品集</h1>
        <a href="/ui/portfolio" className="text-sm text-muted hover:text-ink">進階(排序/批次)→ 舊版</a>
      </header>

      {msg && (
        <p className={`mt-4 rounded-lg px-3 py-2 text-sm ${msg.kind === "ok" ? "bg-ok-soft text-ok" : "bg-danger-soft text-danger"}`}>
          {msg.text}
        </p>
      )}

      <section className="mt-6">
        <h2 className="text-sm font-semibold text-muted">分類</h2>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          {categories.data?.map((c) => (
            <span key={c.id} className="rounded-full border border-line px-3 py-1 text-sm">{c.name}</span>
          ))}
          <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); if (newCat.trim()) addCat.mutate(); }}>
            <input value={newCat} onChange={(e) => setNewCat(e.target.value)} placeholder="新增分類"
              className="w-40 rounded-lg border border-line bg-surface px-3 py-1.5 text-sm" />
            <button disabled={addCat.isPending} className="rounded-lg border border-line px-3 py-1.5 text-sm hover:bg-line/20">新增</button>
          </form>
        </div>
      </section>

      <section className="mt-6">
        <h2 className="text-sm font-semibold text-muted">新增作品</h2>
        <form className="mt-2 flex flex-wrap gap-2" onSubmit={(e) => { e.preventDefault(); if (itemForm.image_url.trim()) addItem.mutate(); }}>
          <input value={itemForm.image_url} onChange={(e) => setItemForm((f) => ({ ...f, image_url: e.target.value }))}
            placeholder="圖片網址" className="w-64 rounded-lg border border-line bg-surface px-3 py-1.5 text-sm" />
          <input value={itemForm.caption} onChange={(e) => setItemForm((f) => ({ ...f, caption: e.target.value }))}
            placeholder="說明(選填)" className="w-48 rounded-lg border border-line bg-surface px-3 py-1.5 text-sm" />
          <select value={itemForm.category_id} onChange={(e) => setItemForm((f) => ({ ...f, category_id: e.target.value }))}
            className="rounded-lg border border-line bg-surface px-3 py-1.5 text-sm">
            <option value="">無分類</option>
            {categories.data?.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
          <button disabled={addItem.isPending} className="rounded-lg bg-brand px-4 py-1.5 text-sm font-semibold text-white hover:bg-brand-deep">新增</button>
        </form>
      </section>

      <div className="mt-6">
        <DataTable columns={columns} rows={items.data} rowKey={(it) => it.id}
          isLoading={items.isLoading} emptyText="尚無作品。" />
      </div>
    </div>
  );
}
