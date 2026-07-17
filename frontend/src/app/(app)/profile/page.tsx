"use client";

import { useEffect, useState } from "react";

import { ApiError, fetchJson, putJson } from "@/lib/client-api";

type Profile = {
  slug: string;
  display_name: string | null;
  theme_color: string | null;
  seo_title: string | null;
  seo_description: string | null;
  intro: string | null;
  announcement: string | null;
  is_published: boolean;
};

const EMPTY: Profile = {
  slug: "",
  display_name: "",
  theme_color: "",
  seo_title: "",
  seo_description: "",
  intro: "",
  announcement: "",
  is_published: false,
};

export default function ProfilePage() {
  const [form, setForm] = useState<Profile>(EMPTY);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  useEffect(() => {
    fetchJson<Profile>("/booking/profile")
      .then((p) => setForm({ ...EMPTY, ...p }))
      .catch((e) => {
        // 404 = 尚未建立店家頁,用空表單。
        if (!(e instanceof ApiError && e.status === 404)) {
          setMsg({ kind: "error", text: "載入失敗,請重試。" });
        }
      })
      .finally(() => setLoading(false));
  }, []);

  function set<K extends keyof Profile>(key: K, value: Profile[K]) {
    setForm((f) => ({ ...f, [key]: value }));
  }

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setMsg(null);
    try {
      const saved = await putJson<Profile>("/booking/profile", form);
      setForm({ ...EMPTY, ...saved });
      setMsg({ kind: "ok", text: "店家頁已儲存。" });
    } catch (err) {
      const text = err instanceof ApiError ? err.detail || `錯誤(${err.status})` : "儲存失敗。";
      setMsg({ kind: "error", text });
    } finally {
      setSaving(false);
    }
  }

  if (loading) return <p className="text-muted">載入中…</p>;

  return (
    <div className="mx-auto max-w-2xl">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">店家頁</h1>
        {form.slug && (
          <a href={`/p/${form.slug}`} target="_blank" rel="noreferrer" className="text-sm text-brand hover:underline">
            預覽公開頁 →
          </a>
        )}
      </header>

      {msg && (
        <p className={`mt-4 rounded-lg px-3 py-2 text-sm ${msg.kind === "ok" ? "bg-ok-soft text-ok" : "bg-danger-soft text-danger"}`}>
          {msg.text}
        </p>
      )}

      <form className="mt-4 grid gap-4" onSubmit={save}>
        <label className="grid gap-1 text-sm font-medium">
          網址代稱(slug)
          <input className="rounded-lg border border-line bg-surface px-3 py-2" value={form.slug}
            onChange={(e) => set("slug", e.target.value)} placeholder="my-shop" />
        </label>
        <label className="grid gap-1 text-sm font-medium">
          店家名稱
          <input className="rounded-lg border border-line bg-surface px-3 py-2" value={form.display_name ?? ""}
            onChange={(e) => set("display_name", e.target.value)} />
        </label>
        <label className="grid gap-1 text-sm font-medium">
          主題色(hex)
          <input className="rounded-lg border border-line bg-surface px-3 py-2" value={form.theme_color ?? ""}
            onChange={(e) => set("theme_color", e.target.value)} placeholder="#C9A36A" />
        </label>
        <label className="grid gap-1 text-sm font-medium">
          SEO 標題
          <input className="rounded-lg border border-line bg-surface px-3 py-2" value={form.seo_title ?? ""}
            onChange={(e) => set("seo_title", e.target.value)} />
        </label>
        <label className="grid gap-1 text-sm font-medium">
          SEO 描述
          <textarea className="rounded-lg border border-line bg-surface px-3 py-2" rows={2} value={form.seo_description ?? ""}
            onChange={(e) => set("seo_description", e.target.value)} />
        </label>
        <label className="grid gap-1 text-sm font-medium">
          介紹
          <textarea className="rounded-lg border border-line bg-surface px-3 py-2" rows={3} value={form.intro ?? ""}
            onChange={(e) => set("intro", e.target.value)} />
        </label>
        <label className="grid gap-1 text-sm font-medium">
          公告
          <textarea className="rounded-lg border border-line bg-surface px-3 py-2" rows={2} value={form.announcement ?? ""}
            onChange={(e) => set("announcement", e.target.value)} />
        </label>
        <label className="flex items-center gap-2 text-sm font-medium">
          <input type="checkbox" checked={form.is_published}
            onChange={(e) => set("is_published", e.target.checked)} />
          公開此頁
        </label>
        <div className="flex items-center gap-3">
          <button disabled={saving} className="rounded-lg bg-brand px-4 py-2 font-semibold text-white hover:bg-brand-deep disabled:opacity-60">
            {saving ? "儲存中…" : "儲存"}
          </button>
          <a href="/ui/profile" className="text-sm text-muted hover:text-ink">進階(橫幅/社群連結)→ 舊版</a>
        </div>
      </form>
    </div>
  );
}
