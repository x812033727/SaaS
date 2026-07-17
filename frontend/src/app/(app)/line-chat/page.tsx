"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { ApiError, fetchJson, postJson } from "@/lib/client-api";

type ConversationRow = {
  line_user_id: string;
  display_name: string;
  last_text: string;
  last_direction: string;
  last_at: string;
};

type MessageRow = {
  id: number;
  line_user_id: string;
  direction: string;
  text: string;
  created_at: string;
};

function errText(error: unknown): string {
  if (error instanceof ApiError) return error.detail || `錯誤(${error.status})`;
  return "操作失敗,請重試。";
}

/** SSE 直連 /ui/events(同網域、SSO 橋 cookie 已種,免 proxy);
 *  斷線由 EventSource 自動重連,錯誤時退回 react-query 的 30s 輪詢。 */
function useLineEvents(onMessage: () => void) {
  const [live, setLive] = useState(false);
  const cbRef = useRef(onMessage);
  cbRef.current = onMessage;
  useEffect(() => {
    const es = new EventSource("/ui/events");
    es.onopen = () => setLive(true);
    es.onerror = () => setLive(false);
    es.addEventListener("line_message", () => cbRef.current());
    return () => es.close();
  }, []);
  return live;
}

export default function LineChatPage() {
  const qc = useQueryClient();
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  const live = useLineEvents(() => {
    qc.invalidateQueries({ queryKey: ["line-conversations"] });
    qc.invalidateQueries({ queryKey: ["line-messages"] });
  });

  const conversations = useQuery({
    queryKey: ["line-conversations"],
    queryFn: () => fetchJson<ConversationRow[]>("/api/v1/line-chat/conversations"),
    retry: false,
    refetchInterval: live ? false : 30_000,
  });

  const messages = useQuery({
    queryKey: ["line-messages", selected],
    queryFn: () =>
      fetchJson<MessageRow[]>(`/api/v1/line-chat/${encodeURIComponent(selected!)}/messages`),
    enabled: selected !== null,
    retry: false,
    refetchInterval: live ? false : 30_000,
  });

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages.data]);

  const replyMut = useMutation({
    mutationFn: (input: { to: string; text: string }) =>
      postJson<MessageRow>(
        `/api/v1/line-chat/${encodeURIComponent(input.to)}/reply`,
        { text: input.text },
      ),
    onSuccess: () => {
      setError(null);
      qc.invalidateQueries({ queryKey: ["line-messages", selected] });
      qc.invalidateQueries({ queryKey: ["line-conversations"] });
    },
    onError: (e) => setError(errText(e)),
  });

  function submitReply(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selected) return;
    const form = event.currentTarget;
    const text = String(new FormData(form).get("text") || "").trim();
    if (!text) return;
    replyMut.mutate({ to: selected, text });
    form.reset();
  }

  const selectedName =
    conversations.data?.find((c) => c.line_user_id === selected)?.display_name || selected;

  return (
    <div className="mx-auto max-w-6xl">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">客服訊息</h1>
        <span className={`rounded-full px-2.5 py-1 text-xs ${
          live ? "bg-ok-soft text-ok" : "bg-line text-muted"
        }`}>{live ? "● 即時連線中" : "○ 輪詢模式(30 秒)"}</span>
      </header>

      <div className="mt-4 grid gap-4 lg:grid-cols-[280px_1fr]">
        <aside className="max-h-[70vh] overflow-y-auto rounded-xl border border-line bg-surface">
          {conversations.isLoading && <p className="p-4 text-sm text-muted">載入中…</p>}
          {conversations.data?.length === 0 && (
            <p className="p-4 text-sm text-muted">尚無對話。顧客傳訊息給 LINE 官方帳號後會出現在這裡。</p>
          )}
          <ul>
            {conversations.data?.map((c) => (
              <li key={c.line_user_id}>
                <button
                  onClick={() => { setSelected(c.line_user_id); setError(null); }}
                  className={`block w-full border-b border-line/60 px-4 py-3 text-left text-sm hover:bg-brand-soft ${
                    selected === c.line_user_id ? "bg-brand-soft" : ""
                  }`}
                >
                  <span className="font-medium">{c.display_name || c.line_user_id}</span>
                  <span className="mt-0.5 block truncate text-xs text-muted">
                    {c.last_direction === "out" && "↩ "}
                    {c.last_text}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </aside>

        <section className="flex max-h-[70vh] flex-col rounded-xl border border-line bg-surface">
          {selected === null ? (
            <p className="p-8 text-center text-sm text-muted">← 選擇一個對話開始回覆。</p>
          ) : (
            <>
              <header className="border-b border-line px-4 py-2.5 text-sm font-semibold">
                {selectedName}
              </header>
              <div className="flex-1 overflow-y-auto p-4">
                {messages.isLoading && <p className="text-sm text-muted">載入中…</p>}
                <ul className="grid gap-2">
                  {messages.data?.map((m) => (
                    <li key={m.id} className={`flex ${m.direction === "out" ? "justify-end" : "justify-start"}`}>
                      <div className={`max-w-[75%] rounded-2xl px-3.5 py-2 text-sm ${
                        m.direction === "out"
                          ? "rounded-br-sm bg-brand text-white"
                          : "rounded-bl-sm bg-bg"
                      }`}>
                        <p className="whitespace-pre-wrap break-words">{m.text}</p>
                        <p className={`mt-0.5 text-right text-[10px] ${
                          m.direction === "out" ? "text-white/70" : "text-muted"
                        }`}>{m.created_at.slice(11, 16)}</p>
                      </div>
                    </li>
                  ))}
                </ul>
                <div ref={bottomRef} />
              </div>
              {error && <p className="px-4 py-1 text-sm text-danger">{error}</p>}
              <form onSubmit={submitReply} className="flex gap-2 border-t border-line p-3">
                <input name="text" required maxLength={2000} placeholder="輸入回覆…"
                  className="flex-1 rounded-lg border border-line px-3 py-2 text-sm" />
                <button disabled={replyMut.isPending}
                  className="rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-white hover:bg-brand-deep disabled:opacity-60">
                  {replyMut.isPending ? "傳送中…" : "回覆"}
                </button>
              </form>
            </>
          )}
        </section>
      </div>
    </div>
  );
}
