"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, delJson, fetchJson, postJson, putJson } from "@/lib/client-api";

type RuleRow = {
  id: number;
  keyword: string;
  match_type: string;
  reply_type: string;
  reply_text: string | null;
  flex_menu_id: number | null;
  priority: number;
  is_active: boolean;
};

type Envelope = {
  rules: RuleRow[];
  flex_menus: { id: number; name: string }[];
  bot_mode: string;
};

const MATCH_LABELS: Record<string, string> = { exact: "精確", prefix: "開頭", contains: "包含" };

function errText(e: unknown): string {
  return e instanceof ApiError ? e.detail || `錯誤(${e.status})` : "操作失敗,請重試。";
}

const inputCls = "mt-1 w-full rounded-lg border border-line bg-surface px-3 py-1.5";
const btnCls =
  "rounded-lg bg-brand px-3 py-1.5 text-sm font-semibold text-white hover:bg-brand-deep disabled:opacity-60";

function RuleForm({
  initial,
  flexMenus,
  onSubmit,
  onCancel,
  pending,
}: {
  initial: RuleRow | null;
  flexMenus: { id: number; name: string }[];
  onSubmit: (body: Record<string, unknown>) => void;
  onCancel?: () => void;
  pending: boolean;
}) {
  const [replyType, setReplyType] = useState(initial?.reply_type ?? "text");

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const f = new FormData(event.currentTarget);
    onSubmit({
      keyword: String(f.get("keyword") ?? ""),
      match_type: String(f.get("match_type")),
      reply_type: replyType,
      reply_text: replyType === "text" ? String(f.get("reply_text") ?? "") : null,
      flex_menu_id: replyType === "flex" ? Number(f.get("flex_menu_id")) : null,
      priority: Number(f.get("priority") ?? 0),
      is_active: initial ? initial.is_active : true,
    });
    if (!initial) {
      event.currentTarget.reset();
      setReplyType("text");
    }
  }

  return (
    <form className="grid gap-2 text-sm" onSubmit={submit}>
      <div className="grid gap-2 sm:grid-cols-3">
        <label>關鍵字
          <input name="keyword" required maxLength={255} defaultValue={initial?.keyword ?? ""} className={inputCls} />
        </label>
        <label>比對方式
          <select name="match_type" defaultValue={initial?.match_type ?? "contains"} className={inputCls}>
            <option value="contains">包含</option>
            <option value="prefix">開頭</option>
            <option value="exact">精確</option>
          </select>
        </label>
        <label>優先序(小者優先)
          <input name="priority" type="number" defaultValue={initial?.priority ?? 0} className={inputCls} />
        </label>
      </div>
      <label>回覆類型
        <select value={replyType} onChange={(e) => setReplyType(e.target.value)} className={inputCls}>
          <option value="text">文字</option>
          <option value="flex">圖文卡片選單</option>
        </select>
      </label>
      {replyType === "text" ? (
        <label>回覆內容
          <textarea name="reply_text" required rows={2} defaultValue={initial?.reply_text ?? ""} className={inputCls} />
        </label>
      ) : (
        <label>圖文卡片選單
          <select name="flex_menu_id" required defaultValue={initial?.flex_menu_id ?? ""} className={inputCls}>
            {flexMenus.map((m) => <option key={m.id} value={m.id}>#{m.id} {m.name}</option>)}
          </select>
          {flexMenus.length === 0 && (
            <span className="mt-1 block text-xs text-warn">尚無圖文卡片選單,請先到「圖文卡片」頁建立。</span>
          )}
        </label>
      )}
      <div className="flex gap-2">
        <button disabled={pending} className={btnCls}>{initial ? "儲存" : "新增規則"}</button>
        {onCancel && (
          <button type="button" onClick={onCancel}
            className="rounded-lg border border-line px-3 py-1.5 hover:bg-line/20">取消</button>
        )}
      </div>
    </form>
  );
}

export default function AutoReplyPage() {
  const qc = useQueryClient();
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [editingId, setEditingId] = useState<number | null>(null);

  const data = useQuery({
    queryKey: ["auto-reply"],
    queryFn: () => fetchJson<Envelope>("/api/v1/auto-reply"),
    retry: false,
  });

  const refresh = () => qc.invalidateQueries({ queryKey: ["auto-reply"] });
  const onErr = (e: unknown) => setMsg({ kind: "error", text: errText(e) });

  const create = useMutation({
    mutationFn: (body: Record<string, unknown>) => postJson("/api/v1/auto-reply", body),
    onSuccess: () => { setMsg(null); refresh(); },
    onError: onErr,
  });
  const update = useMutation({
    mutationFn: (input: { id: number; body: Record<string, unknown> }) =>
      putJson(`/api/v1/auto-reply/${input.id}`, input.body),
    onSuccess: () => { setMsg(null); setEditingId(null); refresh(); },
    onError: onErr,
  });
  const remove = useMutation({
    mutationFn: (id: number) => delJson(`/api/v1/auto-reply/${id}`),
    onSuccess: () => { setMsg(null); refresh(); },
    onError: onErr,
  });

  const env = data.data;

  return (
    <div className="mx-auto max-w-4xl">
      <h1 className="text-2xl font-semibold">LINE 自動回覆</h1>
      <p className="mt-1 text-sm text-muted">
        關鍵字規則:精確 &gt; 開頭 &gt; 包含;同類型優先序小者先命中。
      </p>
      {env && env.bot_mode !== "auto_reply" && (
        <p className="mt-4 rounded-lg bg-warn-soft px-3 py-2 text-sm text-warn">
          目前 Bot 模式為「{env.bot_mode}」,自動回覆規則僅在「auto_reply」模式生效;請至
          <a href="/console/line-settings" className="mx-1 underline">LINE 設定</a>切換。
        </p>
      )}
      {msg && (
        <p className={`mt-4 rounded-lg px-3 py-2 text-sm ${msg.kind === "ok" ? "bg-ok-soft text-ok" : "bg-danger-soft text-danger"}`}>
          {msg.text}
        </p>
      )}
      {data.isLoading && <p className="mt-6 text-sm text-muted">載入中…</p>}

      {env && (
        <>
          <section className="mt-6 rounded-xl border border-line bg-surface p-4">
            <h2 className="font-semibold">新增規則</h2>
            <div className="mt-3">
              <RuleForm initial={null} flexMenus={env.flex_menus}
                onSubmit={(body) => create.mutate(body)} pending={create.isPending} />
            </div>
          </section>

          <div className="mt-4 grid gap-3">
            {env.rules.length === 0 && (
              <p className="rounded-xl border border-line bg-surface p-6 text-sm text-muted">尚無規則。</p>
            )}
            {env.rules.map((r) => (
              <div key={r.id} className="rounded-xl border border-line bg-surface p-4">
                {editingId === r.id ? (
                  <RuleForm initial={r} flexMenus={env.flex_menus}
                    onSubmit={(body) => update.mutate({ id: r.id, body })}
                    onCancel={() => setEditingId(null)} pending={update.isPending} />
                ) : (
                  <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
                    <div>
                      <span className="font-medium">{r.keyword}</span>
                      <span className="ml-2 text-xs text-muted">
                        {MATCH_LABELS[r.match_type] ?? r.match_type}・優先序 {r.priority}
                      </span>
                      <p className="mt-0.5 text-muted">
                        {r.reply_type === "flex"
                          ? `回覆圖文選單 #${r.flex_menu_id}`
                          : r.reply_text}
                      </p>
                    </div>
                    <div className="flex gap-2">
                      <button
                        className={`rounded-full px-2 py-0.5 text-xs ${r.is_active ? "bg-ok-soft text-ok" : "bg-line/40 text-muted"}`}
                        onClick={() =>
                          update.mutate({
                            id: r.id,
                            body: {
                              keyword: r.keyword, match_type: r.match_type,
                              reply_type: r.reply_type, reply_text: r.reply_text,
                              flex_menu_id: r.flex_menu_id, priority: r.priority,
                              is_active: !r.is_active,
                            },
                          })
                        }
                      >
                        {r.is_active ? "啟用中" : "已停用"}
                      </button>
                      <button className="text-brand hover:underline" onClick={() => setEditingId(r.id)}>編輯</button>
                      <button className="text-danger hover:underline"
                        onClick={() => { if (confirm("刪除此規則?")) remove.mutate(r.id); }}>刪除</button>
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
