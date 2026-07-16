"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError, fetchJson, postJson } from "@/lib/client-api";

type ProductRow = { id: number; name: string; price_cents: number; stock: number | null; is_active: boolean };
type StaffRow = { id: number; name: string; is_active: boolean };
type CouponBrief = { id: number; code: string; name: string };
type Lookup = {
  customer_id: number;
  display_name: string | null;
  phone: string | null;
  points_balance: number;
  tier: string;
  tier_discount_percent: number;
  active_coupons: CouponBrief[];
  gift_card_balance_cents: number;
};
type CheckoutResult = {
  id: number; status: string; total_cents: number;
  discount_cents: number; gift_card_cents: number; currency: string;
};
type OrderDetail = CheckoutResult & {
  items: { name_snapshot: string; unit_price_cents: number; qty: number; line_total_cents: number }[];
};

function errText(error: unknown): string {
  if (error instanceof ApiError) return error.detail || `錯誤(${error.status})`;
  return "操作失敗,請重試。";
}

export default function PosPage() {
  const [cart, setCart] = useState<Record<number, number>>({});
  const [phone, setPhone] = useState("");
  const [customer, setCustomer] = useState<Lookup | null>(null);
  const [couponCode, setCouponCode] = useState("");
  const [points, setPoints] = useState(0);
  const [giftCard, setGiftCard] = useState("");
  const [staffId, setStaffId] = useState("");
  const [payMethod, setPayMethod] = useState("cash");
  const [markPaid, setMarkPaid] = useState(true);
  const [receipt, setReceipt] = useState<OrderDetail | null>(null);
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  const products = useQuery({
    queryKey: ["pos-products"],
    queryFn: () => fetchJson<ProductRow[]>("/booking/products/"),
    retry: false,
  });
  const staff = useQuery({
    queryKey: ["pos-staff"],
    queryFn: () => fetchJson<StaffRow[]>("/booking/staff/"),
  });

  const lookupMut = useMutation({
    mutationFn: (p: string) => fetchJson<Lookup>(`/booking/pos/lookup?phone=${encodeURIComponent(p)}`),
    onSuccess: (data) => { setCustomer(data); setMessage(null); },
    onError: (e) => {
      setCustomer(null);
      setMessage({ kind: "error", text: e instanceof ApiError && e.status === 404 ? "查無此顧客(可用散客結帳)" : errText(e) });
    },
  });

  const checkoutMut = useMutation({
    mutationFn: async (body: Record<string, unknown>) => {
      const created = await postJson<CheckoutResult>("/booking/pos/checkout", body);
      return fetchJson<OrderDetail>(`/booking/orders/${created.id}`);
    },
    onSuccess: (order) => {
      setReceipt(order);
      setCart({});
      setCouponCode("");
      setPoints(0);
      setGiftCard("");
      setMessage({ kind: "ok", text: `訂單 #${order.id} 完成。` });
    },
    onError: (e) => setMessage({ kind: "error", text: errText(e) }),
  });

  if (products.error instanceof ApiError && products.error.status === 403) {
    return (
      <div className="mx-auto max-w-4xl">
        <h1 className="text-2xl font-semibold">POS 結帳</h1>
        <div className="mt-6 rounded-xl border border-line bg-warn-soft p-6 text-sm">
          <p className="font-semibold text-warn">此功能未啟用</p>
          <p className="mt-2">商品銷售屬進階功能,請至<a href="/ui/plan" className="mx-1 text-brand underline">方案頁</a>啟用。</p>
        </div>
      </div>
    );
  }

  const activeProducts = (products.data ?? []).filter((p) => p.is_active);
  const cartItems = Object.entries(cart)
    .map(([pid, qty]) => ({ product: activeProducts.find((p) => p.id === Number(pid)), qty }))
    .filter((x): x is { product: ProductRow; qty: number } => !!x.product && x.qty > 0);
  const subtotal = cartItems.reduce((s, x) => s + x.product.price_cents * x.qty, 0);

  function setQty(pid: number, qty: number) {
    setCart((c) => ({ ...c, [pid]: Math.max(0, qty) }));
  }

  function submit() {
    if (cartItems.length === 0) {
      setMessage({ kind: "error", text: "購物車是空的。" });
      return;
    }
    setReceipt(null);
    checkoutMut.mutate({
      customer_id: customer?.customer_id ?? null,
      items: cartItems.map((x) => ({ product_id: x.product.id, qty: x.qty })),
      ...(couponCode.trim() ? { coupon_code: couponCode.trim() } : {}),
      ...(points > 0 ? { points_to_redeem: points } : {}),
      ...(giftCard.trim() ? { gift_card_code: giftCard.trim() } : {}),
      ...(staffId ? { staff_id: Number(staffId) } : {}),
      payment_method: payMethod,
      mark_paid: markPaid,
    });
  }

  return (
    <div className="mx-auto max-w-6xl">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">POS 結帳</h1>
        <a href="/ui/pos" className="text-sm text-muted hover:text-ink">舊版頁面</a>
      </header>

      {message && (
        <p className={`mt-3 rounded-lg px-3 py-2 text-sm ${
          message.kind === "ok" ? "bg-ok-soft text-ok" : "bg-danger-soft text-danger"
        }`}>{message.text}</p>
      )}

      <div className="mt-4 grid gap-4 lg:grid-cols-[1fr_360px]">
        {/* 商品挑選 */}
        <section className="rounded-xl border border-line bg-surface p-5">
          <h2 className="font-semibold">商品</h2>
          {products.isLoading && <p className="mt-4 text-sm text-muted">載入中…</p>}
          {activeProducts.length === 0 && !products.isLoading && (
            <p className="mt-4 text-sm text-muted">尚無上架商品。<a href="/ui/shop" className="text-brand underline">去建立</a></p>
          )}
          <div className="mt-3 grid gap-2 sm:grid-cols-2">
            {activeProducts.map((p) => (
              <div key={p.id} className="flex items-center justify-between gap-2 rounded-lg border border-line px-3 py-2 text-sm">
                <div className="min-w-0">
                  <p className="truncate font-medium">{p.name}</p>
                  <p className="text-xs text-muted">
                    NT${Math.floor(p.price_cents / 100).toLocaleString()}
                    {p.stock != null && ` · 庫存 ${p.stock}`}
                  </p>
                </div>
                <div className="flex items-center gap-1">
                  <button onClick={() => setQty(p.id, (cart[p.id] ?? 0) - 1)}
                    className="h-7 w-7 rounded-md border border-line">−</button>
                  <span className="w-6 text-center">{cart[p.id] ?? 0}</span>
                  <button onClick={() => setQty(p.id, (cart[p.id] ?? 0) + 1)}
                    className="h-7 w-7 rounded-md border border-line bg-brand-soft">+</button>
                </div>
              </div>
            ))}
          </div>
        </section>

        {/* 結帳欄 */}
        <section className="space-y-4">
          <div className="rounded-xl border border-line bg-surface p-5 text-sm">
            <h2 className="font-semibold">會員(選填)</h2>
            <form className="mt-2 flex gap-2" onSubmit={(e) => { e.preventDefault(); if (phone.trim()) lookupMut.mutate(phone.trim()); }}>
              <input value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="電話查詢"
                className="min-w-0 flex-1 rounded-lg border border-line px-3 py-2" />
              <button className="rounded-lg border border-line px-3 py-2 hover:bg-brand-soft">查詢</button>
            </form>
            {customer && (
              <div className="mt-3 rounded-lg bg-brand-soft p-3">
                <div className="flex items-center justify-between">
                  <p className="font-medium">{customer.display_name ?? `顧客 #${customer.customer_id}`}</p>
                  <button onClick={() => setCustomer(null)} className="text-xs text-muted hover:text-ink">改散客</button>
                </div>
                <p className="mt-1 text-xs text-muted">
                  點數 {customer.points_balance} · {customer.tier}
                  {customer.tier_discount_percent > 0 && ` · 會員 ${customer.tier_discount_percent}% off`}
                  {customer.gift_card_balance_cents > 0 &&
                    ` · 禮卡 NT$${Math.floor(customer.gift_card_balance_cents / 100)}`}
                </p>
                {customer.active_coupons.length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-1">
                    {customer.active_coupons.map((c) => (
                      <button key={c.id} onClick={() => setCouponCode(c.code)}
                        className="rounded-full bg-gold-soft px-2 py-0.5 text-xs hover:bg-gold hover:text-white">
                        {c.name}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>

          <div className="rounded-xl border border-line bg-surface p-5 text-sm">
            <h2 className="font-semibold">結帳</h2>
            <ul className="mt-2 space-y-1">
              {cartItems.map((x) => (
                <li key={x.product.id} className="flex justify-between">
                  <span>{x.product.name} ×{x.qty}</span>
                  <span>NT${Math.floor((x.product.price_cents * x.qty) / 100).toLocaleString()}</span>
                </li>
              ))}
              {cartItems.length === 0 && <li className="text-muted">尚未選擇商品</li>}
            </ul>
            <p className="mt-2 border-t border-line pt-2 text-right font-semibold">
              小計 NT${Math.floor(subtotal / 100).toLocaleString()}
            </p>
            <div className="mt-3 grid gap-2">
              <input value={couponCode} onChange={(e) => setCouponCode(e.target.value)}
                placeholder="優惠券代碼" className="rounded-lg border border-line px-3 py-2" />
              <div className="grid grid-cols-2 gap-2">
                <input type="number" min={0} value={points || ""} disabled={!customer}
                  onChange={(e) => setPoints(Number(e.target.value) || 0)}
                  placeholder="折抵點數" className="rounded-lg border border-line px-3 py-2 disabled:opacity-50" />
                <input value={giftCard} onChange={(e) => setGiftCard(e.target.value)}
                  placeholder="禮卡代碼" className="rounded-lg border border-line px-3 py-2" />
              </div>
              <div className="grid grid-cols-2 gap-2">
                <select value={staffId} onChange={(e) => setStaffId(e.target.value)}
                  className="rounded-lg border border-line px-3 py-2">
                  <option value="">員工歸屬(選填)</option>
                  {(staff.data ?? []).filter((s) => s.is_active).map((s) => (
                    <option key={s.id} value={s.id}>{s.name}</option>
                  ))}
                </select>
                <select value={payMethod} onChange={(e) => setPayMethod(e.target.value)}
                  className="rounded-lg border border-line px-3 py-2">
                  <option value="cash">現金</option>
                  <option value="card">刷卡</option>
                  <option value="transfer">轉帳</option>
                </select>
              </div>
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={markPaid} onChange={(e) => setMarkPaid(e.target.checked)} />
                立即標記已收款
              </label>
              <button onClick={submit} disabled={checkoutMut.isPending || cartItems.length === 0}
                className="rounded-lg bg-brand px-4 py-2.5 font-semibold text-white hover:bg-brand-deep disabled:opacity-50">
                {checkoutMut.isPending ? "結帳中…" : "結帳"}
              </button>
            </div>
          </div>

          {receipt && (
            <div className="rounded-xl border border-ok bg-ok-soft p-5 text-sm">
              <h2 className="font-semibold text-ok">單據 #{receipt.id}</h2>
              <ul className="mt-2 space-y-1">
                {receipt.items.map((it, i) => (
                  <li key={i} className="flex justify-between">
                    <span>{it.name_snapshot} ×{it.qty}</span>
                    <span>NT${Math.floor(it.line_total_cents / 100).toLocaleString()}</span>
                  </li>
                ))}
              </ul>
              {(receipt.discount_cents > 0 || receipt.gift_card_cents > 0) && (
                <p className="mt-1 text-xs text-muted">
                  折抵 NT${Math.floor((receipt.discount_cents + receipt.gift_card_cents) / 100).toLocaleString()}
                </p>
              )}
              <p className="mt-2 border-t border-ok/30 pt-2 text-right font-semibold">
                實收 NT${Math.floor(receipt.total_cents / 100).toLocaleString()}
                <span className="ml-2 text-xs">({receipt.status === "paid" ? "已收款" : "待付款"})</span>
              </p>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
