import { redirect } from "next/navigation";
import { apiFetch, type AppContext } from "@/lib/api";

export default async function DashboardPage() {
  let context: AppContext;
  try {
    context = await apiFetch<AppContext>("/api/v1/context");
  } catch (error) {
    if (error instanceof Error && error.message === "UNAUTHENTICATED") redirect("/login");
    throw error;
  }

  const cards = [
    ["組織", context.organization.name],
    ["目前品牌", context.tenant.name],
    ["組織角色", context.organization.role],
    ["可用權限", String(context.permissions.length)],
  ];
  return (
    <main className="min-h-screen px-5 py-8 lg:px-12">
      <header className="mx-auto flex max-w-6xl items-center justify-between gap-4">
        <div>
          <p className="text-sm font-semibold tracking-[0.16em] text-[var(--accent)]">SERVICE OS</p>
          <h1 className="mt-1 text-3xl font-semibold">營運總覽</h1>
        </div>
        <form action="/api/session/logout" method="post"><button className="rounded-xl border border-[var(--line)] bg-white px-4 py-2 text-sm">登出</button></form>
      </header>
      <section className="mx-auto mt-10 grid max-w-6xl gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {cards.map(([label, value]) => (
          <article key={label} className="rounded-2xl border border-[var(--line)] bg-white p-5 shadow-sm">
            <p className="text-sm text-[var(--muted)]">{label}</p>
            <p className="mt-3 text-2xl font-semibold text-[var(--brand)]">{value}</p>
          </article>
        ))}
      </section>
      <section className="mx-auto mt-6 max-w-6xl rounded-2xl border border-[var(--line)] bg-white p-6">
        <h2 className="text-lg font-semibold">跨品牌共享政策</h2>
        <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-3">
          <div><dt className="text-[var(--muted)]">顧客</dt><dd className="font-medium">{context.organization.share_customers ? "已開啟" : "未開啟"}</dd></div>
          <div><dt className="text-[var(--muted)]">會員點數</dt><dd className="font-medium">{context.organization.share_loyalty ? "已開啟" : "未開啟"}</dd></div>
          <div><dt className="text-[var(--muted)]">票券</dt><dd className="font-medium">{context.organization.share_coupons ? "已開啟" : "未開啟"}</dd></div>
        </dl>
      </section>
    </main>
  );
}
