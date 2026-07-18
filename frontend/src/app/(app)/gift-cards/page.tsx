"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, fetchJson, postJson } from "@/lib/client-api";
import { DataTable, type Column } from "@/components/ui/DataTable";

type GiftCardRow = {
  id: number;
  code_last4: string;
  status: string;
  initial_value_cents: number;
  balance_cents: number;
  recipient_customer_id: number | null;
  purchaser_name: string | null;
  recipient_name: string | null;
  message: string | null;
  created_at: string;
};

type GiftCardIssued = { card: GiftCardRow; code: string | null; created: boolean };

function errText(e: unknown): string {
  return e instanceof ApiError ? e.detail || `錯誤(${e.status})` : "操作失敗,請重試。";
}

function newIssuanceKey(): string {
  return crypto.randomUUID().replace(/-/g, "");
}

function twd(cents: number): string {
  return `NT$${(cents / 100).toLocaleString()}`;
}

function FeatureLockedCard() {
  return (
    <div className="rounded-xl border border-line bg-warn-soft p-6 text-sm">
      <p className="font-semibold text-warn">此功能未啟用</p>
      <p className="mt-2 text-ink">
        電子禮物卡屬進階功能,請至
        <a href="/ui/plan" className="mx-1 text-brand underline">方案頁</a>
        升級或啟用後再試。
      </p>
    </div>
  );
}

export default function GiftCardsPage() {
  const qc = useQueryClient();
  const [issuanceKey, setIssuanceKey] = useState(newIssuanceKey);
  const [issuedCode, setIssuedCode] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  const cards = useQuery({
    queryKey: ["gift-cards"],
    queryFn: () => fetchJson<GiftCardRow[]>("/api/v1/gift-cards"),
    retry: false,
  });

  const issue = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      postJson<GiftCardIssued>("/api/v1/gift-cards", { ...body, issuance_key: issuanceKey }),
    onSuccess: (r) => {
      setIssuanceKey(newIssuanceKey());
      setIssuedCode(r.code);
      setMsg({ kind: "ok", text: r.created ? "禮物卡已發行。" : "此發行識別碼已處理過(未重複發卡)。" });
      qc.invalidateQueries({ queryKey: ["gift-cards"] });
    },
    onError: (e) => setMsg({ kind: "error", text: errText(e) }),
  });

  const voidMut = useMutation({
    mutationFn: (input: { id: number; note: string }) =>
      postJson<GiftCardRow>(`/api/v1/gift-cards/${input.id}/void`, { note: input.note }),
    onSuccess: () => {
      setMsg({ kind: "ok", text: "禮物卡已作廢,剩餘餘額已沖銷。" });
      qc.invalidateQueries({ queryKey: ["gift-cards"] });
    },
    onError: (e) => setMsg({ kind: "error", text: errText(e) }),
  });

  if (cards.error instanceof ApiError && cards.error.status === 403) {
    return (
      <div className="mx-auto max-w-5xl">
        <h1 className="text-2xl font-semibold">電子禮物卡</h1>
        <div className="mt-6"><FeatureLockedCard /></div>
      </div>
    );
  }

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const recipientId = String(form.get("recipient_customer_id") ?? "").trim();
    issue.mutate({
      amount_twd: Number(form.get("amount_twd")),
      fulfillment_guarantee: String(form.get("fulfillment_guarantee") ?? ""),
      purchaser_name: String(form.get("purchaser_name") ?? ""),
      recipient_name: String(form.get("recipient_name") ?? ""),
      message: String(form.get("message") ?? ""),
      recipient_customer_id: recipientId ? Number(recipientId) : null,
      compliance_ack: form.get("compliance_ack") === "on",
    });
  }

  const columns: Column<GiftCardRow>[] = [
    { header: "#", cell: (c) => c.id },
    { header: "卡號末四碼", cell: (c) => <code className="text-sm">****{c.code_last4}</code> },
    { header: "面額", cell: (c) => twd(c.initial_value_cents) },
    { header: "餘額", cell: (c) => <span className="font-medium">{twd(c.balance_cents)}</span> },
    {
      header: "狀態",
      cell: (c) => (
        <span className={c.status === "void" ? "text-muted" : "text-ok"}>
          {c.status === "void" ? "已作廢" : "有效"}
        </span>
      ),
    },
    { header: "購買人/收禮人", className: "text-muted", cell: (c) => `${c.purchaser_name ?? "—"} / ${c.recipient_name ?? "—"}` },
    { header: "發行日", className: "text-muted", cell: (c) => c.created_at.slice(0, 10) },
    {
      header: "", className: "text-right",
      cell: (c) =>
        c.status !== "void" ? (
          <button
            className="text-sm text-danger hover:underline"
            onClick={() => {
              const note = prompt("作廢原因(將沖銷剩餘餘額,請先完成退款):");
              if (note !== null) voidMut.mutate({ id: c.id, note });
            }}
          >
            作廢
          </button>
        ) : (
          <span className="text-muted">—</span>
        ),
    },
  ];

  return (
    <div className="mx-auto max-w-5xl">
      <h1 className="text-2xl font-semibold">電子禮物卡</h1>
      <p className="mt-1 text-sm text-muted">
        發行儲值禮物卡;卡號明碼僅在發行時顯示一次,請立即抄送給購買人。餘額由帳目累計,POS 結帳可折抵。
      </p>
      {msg && (
        <p className={`mt-4 rounded-lg px-3 py-2 text-sm ${msg.kind === "ok" ? "bg-ok-soft text-ok" : "bg-danger-soft text-danger"}`}>
          {msg.text}
        </p>
      )}

      {issuedCode && (
        <div className="mt-4 rounded-lg border border-line bg-ok-soft p-3 text-sm">
          <p className="font-semibold text-ok">卡號(僅顯示這一次,請立即保存):</p>
          <code className="mt-1 block break-all text-lg">{issuedCode}</code>
        </div>
      )}

      <form className="mt-6 grid gap-3 rounded-xl border border-line bg-surface p-4" onSubmit={submit}>
        <h2 className="font-semibold">發行禮物卡</h2>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="text-sm">
            面額(NT$,100～1,000,000)
            <input name="amount_twd" type="number" min={100} max={1000000} required
              className="mt-1 w-full rounded-lg border border-line bg-surface px-3 py-2" />
          </label>
          <label className="text-sm">
            收禮顧客編號(選填,綁定後顧客可直接使用)
            <input name="recipient_customer_id" type="number" min={1}
              className="mt-1 w-full rounded-lg border border-line bg-surface px-3 py-2" />
          </label>
          <label className="text-sm">
            購買人姓名(選填)
            <input name="purchaser_name" maxLength={128}
              className="mt-1 w-full rounded-lg border border-line bg-surface px-3 py-2" />
          </label>
          <label className="text-sm">
            收禮人姓名(選填)
            <input name="recipient_name" maxLength={128}
              className="mt-1 w-full rounded-lg border border-line bg-surface px-3 py-2" />
          </label>
        </div>
        <label className="text-sm">
          祝福訊息(選填)
          <input name="message" maxLength={500}
            className="mt-1 w-full rounded-lg border border-line bg-surface px-3 py-2" />
        </label>
        <label className="text-sm">
          履約保障資訊(10～2,000 字;禮券法規要求,發行當下存檔供稽核)
          <textarea name="fulfillment_guarantee" required minLength={10} maxLength={2000} rows={3}
            className="mt-1 w-full rounded-lg border border-line bg-surface px-3 py-2"
            placeholder="例:本店禮物卡已依法辦理履約保證,由○○銀行提供價金保管…" />
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input name="compliance_ack" type="checkbox" required />
          我已核對履約保障與禮券法規資訊
        </label>
        <div>
          <button disabled={issue.isPending}
            className="rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-white hover:bg-brand-deep disabled:opacity-60">
            發行
          </button>
        </div>
      </form>

      <div className="mt-6">
        <DataTable columns={columns} rows={cards.data} rowKey={(c) => c.id}
          isLoading={cards.isLoading} emptyText="尚未發行禮物卡。" minWidth={780} />
      </div>
    </div>
  );
}
