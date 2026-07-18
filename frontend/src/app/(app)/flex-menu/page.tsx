"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { ApiError, delJson, fetchJson, postJson, putJson } from "@/lib/client-api";

type Menu = { id: number; title: string | null; is_active: boolean };
type Card = {
  id: number;
  title: string;
  action_type: string;
  action_data: string;
  subtitle: string | null;
  image_url: string | null;
  bg_color: string | null;
};

const ACTION_LABELS: Record<string, string> = { uri: "開啟網址", message: "送出訊息", postback: "回傳資料" };
const MAX_CARDS = 12;

function errText(e: unknown): string {
  return e instanceof ApiError ? e.detail || `錯誤(${e.status})` : "操作失敗,請重試。";
}

function FeatureLockedCard() {
  return (
    <div className="rounded-xl border border-line bg-warn-soft p-6 text-sm">
      <p className="font-semibold text-warn">此功能未啟用</p>
      <p className="mt-2 text-ink">
        圖文卡片選單屬進階功能,請至
        <a href="/plan" className="mx-1 text-brand underline">方案頁</a>
        升級或啟用後再試。
      </p>
    </div>
  );
}

const inputCls = "mt-1 w-full rounded-lg border border-line bg-surface px-3 py-1.5";
const btnCls =
  "rounded-lg bg-brand px-3 py-1.5 text-sm font-semibold text-white hover:bg-brand-deep disabled:opacity-60";

function CardForm({
  initial,
  onSubmit,
  onCancel,
  pending,
}: {
  initial: Card | null;
  onSubmit: (body: Record<string, unknown>) => void;
  onCancel?: () => void;
  pending: boolean;
}) {
  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const f = new FormData(event.currentTarget);
    onSubmit({
      title: String(f.get("title") ?? ""),
      subtitle: String(f.get("subtitle") ?? ""),
      image_url: String(f.get("image_url") ?? ""),
      bg_color: String(f.get("bg_color") ?? ""),
      action_type: String(f.get("action_type")),
      action_data: String(f.get("action_data") ?? ""),
    });
    if (!initial) event.currentTarget.reset();
  }

  return (
    <form className="grid gap-2 text-sm" onSubmit={submit}>
      <div className="grid gap-2 sm:grid-cols-2">
        <label>標題
          <input name="title" required maxLength={128} defaultValue={initial?.title ?? ""} className={inputCls} />
        </label>
        <label>副標(選填)
          <input name="subtitle" maxLength={256} defaultValue={initial?.subtitle ?? ""} className={inputCls} />
        </label>
        <label>圖片網址(選填,https)
          <input name="image_url" maxLength={512} defaultValue={initial?.image_url ?? ""} className={inputCls} />
        </label>
        <label>背景色(選填,如 #10b981)
          <input name="bg_color" maxLength={16} defaultValue={initial?.bg_color ?? ""} className={inputCls} />
        </label>
        <label>按鈕動作
          <select name="action_type" defaultValue={initial?.action_type ?? "message"} className={inputCls}>
            <option value="message">送出訊息</option>
            <option value="uri">開啟網址</option>
            <option value="postback">回傳資料</option>
          </select>
        </label>
        <label>動作內容(訊息文字/網址/資料)
          <input name="action_data" required maxLength={512} defaultValue={initial?.action_data ?? ""} className={inputCls} />
        </label>
      </div>
      <div className="flex gap-2">
        <button disabled={pending} className={btnCls}>{initial ? "儲存" : "加入卡片"}</button>
        {onCancel && (
          <button type="button" onClick={onCancel}
            className="rounded-lg border border-line px-3 py-1.5 hover:bg-line/20">取消</button>
        )}
      </div>
    </form>
  );
}

export default function FlexMenuPage() {
  const qc = useQueryClient();
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [editingCardId, setEditingCardId] = useState<number | null>(null);

  // 單一選單 UX:取 active-or-first,沒有就建一個(鏡射 /ui 的 get-or-create)
  const menus = useQuery({
    queryKey: ["flex-menus"],
    queryFn: () => fetchJson<Menu[]>("/booking/flex-menu/"),
    retry: false,
  });
  const ensure = useMutation({
    mutationFn: () => postJson<Menu>("/booking/flex-menu/", {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["flex-menus"] }),
    onError: (e) => setMsg({ kind: "error", text: errText(e) }),
  });
  const menu = menus.data?.find((m) => m.is_active) ?? menus.data?.[0] ?? null;
  const needCreate = menus.data !== undefined && menus.data.length === 0;
  useEffect(() => {
    if (needCreate && !ensure.isPending && !ensure.isSuccess) ensure.mutate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [needCreate]);

  const cards = useQuery({
    queryKey: ["flex-cards", menu?.id],
    queryFn: () => fetchJson<Card[]>(`/booking/flex-menu/${menu!.id}/cards`),
    enabled: menu !== null,
    retry: false,
  });

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["flex-menus"] });
    qc.invalidateQueries({ queryKey: ["flex-cards"] });
  };
  const onErr = (e: unknown) => setMsg({ kind: "error", text: errText(e) });

  const saveTitle = useMutation({
    mutationFn: (title: string) => putJson(`/booking/flex-menu/${menu!.id}`, { title }),
    onSuccess: () => { setMsg({ kind: "ok", text: "選單標題已更新。" }); refresh(); },
    onError: onErr,
  });
  const addCard = useMutation({
    mutationFn: (body: Record<string, unknown>) => postJson(`/booking/flex-menu/${menu!.id}/cards`, body),
    onSuccess: () => { setMsg(null); refresh(); },
    onError: onErr,
  });
  const updateCard = useMutation({
    mutationFn: (input: { id: number; body: Record<string, unknown> }) =>
      putJson(`/booking/flex-menu/${menu!.id}/cards/${input.id}`, input.body),
    onSuccess: () => { setMsg(null); setEditingCardId(null); refresh(); },
    onError: onErr,
  });
  const deleteCard = useMutation({
    mutationFn: (id: number) => delJson<void>(`/booking/flex-menu/${menu!.id}/cards/${id}`),
    onSuccess: () => { setMsg(null); refresh(); },
    onError: onErr,
  });
  const resetMenu = useMutation({
    mutationFn: () => delJson<void>(`/booking/flex-menu/${menu!.id}`),
    onSuccess: () => { setMsg({ kind: "ok", text: "選單已重設。" }); refresh(); },
    onError: onErr,
  });

  if (menus.error instanceof ApiError && menus.error.status === 403) {
    return (
      <div className="mx-auto max-w-4xl">
        <h1 className="text-2xl font-semibold">圖文卡片選單</h1>
        <div className="mt-6"><FeatureLockedCard /></div>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-4xl">
      <h1 className="text-2xl font-semibold">圖文卡片選單</h1>
      <p className="mt-1 text-sm text-muted">
        LINE 對話中的輪播卡片(最多 {MAX_CARDS} 張);由自動回覆規則或歡迎訊息觸發送出。
      </p>
      {msg && (
        <p className={`mt-4 rounded-lg px-3 py-2 text-sm ${msg.kind === "ok" ? "bg-ok-soft text-ok" : "bg-danger-soft text-danger"}`}>
          {msg.text}
        </p>
      )}
      {(menus.isLoading || (menu === null && !menus.error)) && (
        <p className="mt-6 text-sm text-muted">載入中…</p>
      )}

      {menu && (
        <>
          <section className="mt-6 rounded-xl border border-line bg-surface p-4">
            <form className="flex flex-wrap items-end gap-2 text-sm"
              onSubmit={(e) => {
                e.preventDefault();
                saveTitle.mutate(String(new FormData(e.currentTarget).get("title") ?? ""));
              }}>
              <label className="grow">選單標題(選填)
                <input name="title" maxLength={128} defaultValue={menu.title ?? ""} className={inputCls} />
              </label>
              <button disabled={saveTitle.isPending} className={btnCls}>儲存標題</button>
              <button type="button"
                className="rounded-lg border border-line px-3 py-1.5 text-danger hover:bg-danger-soft"
                onClick={() => { if (confirm("重設整個選單?所有卡片將被刪除。")) resetMenu.mutate(); }}>
                重設選單
              </button>
            </form>
          </section>

          <section className="mt-4 rounded-xl border border-line bg-surface p-4">
            <h2 className="font-semibold">
              卡片({cards.data?.length ?? 0}/{MAX_CARDS})
            </h2>
            <div className="mt-3 grid gap-3">
              {cards.data?.length === 0 && <p className="text-sm text-muted">尚無卡片。</p>}
              {cards.data?.map((c) => (
                <div key={c.id} className="rounded-lg border border-line/60 p-3">
                  {editingCardId === c.id ? (
                    <CardForm initial={c}
                      onSubmit={(body) => updateCard.mutate({ id: c.id, body })}
                      onCancel={() => setEditingCardId(null)} pending={updateCard.isPending} />
                  ) : (
                    <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
                      <div className="flex items-center gap-3">
                        {c.image_url ? (
                          // eslint-disable-next-line @next/next/no-img-element
                          <img src={c.image_url} alt="" className="h-12 w-12 rounded-lg object-cover" />
                        ) : (
                          <div className="h-12 w-12 rounded-lg" style={{ background: c.bg_color || "#e5e7eb" }} />
                        )}
                        <div>
                          <p className="font-medium">{c.title}</p>
                          <p className="text-xs text-muted">
                            {c.subtitle ? `${c.subtitle}・` : ""}
                            {ACTION_LABELS[c.action_type] ?? c.action_type}:{c.action_data}
                          </p>
                        </div>
                      </div>
                      <div className="flex gap-2">
                        <button className="text-brand hover:underline" onClick={() => setEditingCardId(c.id)}>編輯</button>
                        <button className="text-danger hover:underline"
                          onClick={() => { if (confirm("刪除此卡片?")) deleteCard.mutate(c.id); }}>刪除</button>
                      </div>
                    </div>
                  )}
                </div>
              ))}
            </div>
            {(cards.data?.length ?? 0) < MAX_CARDS && (
              <div className="mt-4 border-t border-line/60 pt-3">
                <h3 className="text-sm font-medium">加入卡片</h3>
                <div className="mt-2">
                  <CardForm initial={null} onSubmit={(body) => addCard.mutate(body)} pending={addCard.isPending} />
                </div>
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}
