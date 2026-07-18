"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, delJson, fetchJson, postJson } from "@/lib/client-api";

type MemberRow = {
  id: number;
  email: string;
  role: string;
  disabled: boolean;
  is_self: boolean;
};

function errText(e: unknown): string {
  return e instanceof ApiError ? e.detail || `錯誤(${e.status})` : "操作失敗,請重試。";
}

export default function MembersPage() {
  const qc = useQueryClient();
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [inviteUrl, setInviteUrl] = useState<string | null>(null);

  const members = useQuery({
    queryKey: ["members"],
    queryFn: () => fetchJson<MemberRow[]>("/api/v1/members"),
    retry: false,
  });

  const refresh = () => qc.invalidateQueries({ queryKey: ["members"] });
  const onErr = (e: unknown) => setMsg({ kind: "error", text: errText(e) });
  const onRows = (rows: MemberRow[], text: string) => {
    qc.setQueryData(["members"], rows);
    setMsg({ kind: "ok", text });
  };

  const invite = useMutation({
    mutationFn: () => postJson<{ invite_url: string }>("/api/v1/members/invite", {}),
    onSuccess: (r) => { setInviteUrl(r.invite_url); setMsg(null); },
    onError: onErr,
  });
  const setRole = useMutation({
    mutationFn: (input: { id: number; role: string }) =>
      postJson<MemberRow[]>(`/api/v1/members/${input.id}/role`, { role: input.role }),
    onSuccess: (rows) => onRows(rows, "角色已更新。"),
    onError: onErr,
  });
  const toggle = useMutation({
    mutationFn: (input: { id: number; disabled: boolean }) =>
      postJson<MemberRow[]>(
        `/api/v1/members/${input.id}/${input.disabled ? "enable" : "disable"}`, {},
      ),
    onSuccess: (rows, input) =>
      onRows(rows, input.disabled ? "成員已啟用。" : "成員已停用,其所有登入即刻失效。"),
    onError: onErr,
  });
  const remove = useMutation({
    mutationFn: (id: number) => delJson<MemberRow[]>(`/api/v1/members/${id}`),
    onSuccess: (rows) => onRows(rows, "成員已移除。"),
    onError: onErr,
  });

  if (members.error instanceof ApiError && members.error.status === 403) {
    return (
      <div className="mx-auto max-w-3xl">
        <h1 className="text-2xl font-semibold">成員管理</h1>
        <p className="mt-6 rounded-xl border border-line bg-surface p-6 text-sm text-muted">
          成員管理僅限負責人。
        </p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-3xl">
      <h1 className="text-2xl font-semibold">成員管理</h1>
      <p className="mt-1 text-sm text-muted">
        負責人可管理帳務/LINE 設定/成員;員工處理日常營運。至少須保留一位有效負責人。
      </p>
      {msg && (
        <p className={`mt-4 rounded-lg px-3 py-2 text-sm ${msg.kind === "ok" ? "bg-ok-soft text-ok" : "bg-danger-soft text-danger"}`}>
          {msg.text}
        </p>
      )}

      <section className="mt-6 rounded-xl border border-line bg-surface p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="font-semibold">邀請成員</h2>
          <button disabled={invite.isPending}
            className="rounded-lg bg-brand px-3 py-1.5 text-sm font-semibold text-white hover:bg-brand-deep disabled:opacity-60"
            onClick={() => invite.mutate()}>
            產生邀請連結
          </button>
        </div>
        {inviteUrl && (
          <div className="mt-3 rounded-lg border border-line bg-ok-soft p-3 text-sm">
            <p className="font-semibold text-ok">邀請連結(7 天內有效,受邀者自設帳密加入為員工):</p>
            <code className="mt-1 block break-all">{inviteUrl}</code>
            <button className="mt-2 text-xs text-brand hover:underline"
              onClick={() => navigator.clipboard?.writeText(inviteUrl)}>
              複製連結
            </button>
          </div>
        )}
      </section>

      <div className="mt-4 overflow-x-auto rounded-xl border border-line bg-surface">
        <table className="w-full text-sm" style={{ minWidth: 560 }}>
          <thead>
            <tr className="border-b border-line text-left text-muted">
              <th className="px-4 py-2.5 font-medium">Email</th>
              <th className="px-4 py-2.5 font-medium">角色</th>
              <th className="px-4 py-2.5 font-medium">狀態</th>
              <th className="px-4 py-2.5 font-medium"></th>
            </tr>
          </thead>
          <tbody>
            {members.isLoading && (
              <tr><td colSpan={4} className="px-4 py-8 text-center text-muted">載入中…</td></tr>
            )}
            {members.data?.map((m) => (
              <tr key={m.id} className="border-b border-line/60">
                <td className="px-4 py-2.5">
                  {m.email}
                  {m.is_self && <span className="ml-1 text-xs text-muted">(您)</span>}
                </td>
                <td className="px-4 py-2.5">{m.role === "owner" ? "負責人" : "員工"}</td>
                <td className="px-4 py-2.5">
                  <span className={m.disabled ? "text-danger" : "text-ok"}>
                    {m.disabled ? "已停用" : "有效"}
                  </span>
                </td>
                <td className="px-4 py-2.5 text-right">
                  {!m.is_self && (
                    <div className="flex justify-end gap-2 text-xs">
                      <button className="text-brand hover:underline"
                        onClick={() => setRole.mutate({ id: m.id, role: m.role === "owner" ? "staff" : "owner" })}>
                        設為{m.role === "owner" ? "員工" : "負責人"}
                      </button>
                      <button className={m.disabled ? "text-ok hover:underline" : "text-warn hover:underline"}
                        onClick={() => {
                          if (m.disabled || confirm(`停用 ${m.email}?其所有登入將即刻失效。`))
                            toggle.mutate({ id: m.id, disabled: m.disabled });
                        }}>
                        {m.disabled ? "啟用" : "停用"}
                      </button>
                      <button className="text-danger hover:underline"
                        onClick={() => { if (confirm(`永久移除 ${m.email}?此動作不可復原。`)) remove.mutate(m.id); }}>
                        移除
                      </button>
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
