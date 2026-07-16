import Link from "next/link";

import { NAV_GROUPS, NAV_TOP } from "@/lib/nav";
import type { AppContext } from "@/lib/api";

/** 後台外殼:精品風側欄(同構 /ui 分組)+ 頂欄。Server Component。 */
export default function AppShell({
  context,
  children,
}: {
  context: AppContext;
  children: React.ReactNode;
}) {
  return (
    <div className="flex min-h-screen">
      <aside className="hidden w-60 shrink-0 flex-col border-r border-line bg-surface lg:flex">
        <div className="border-b border-line px-5 py-4">
          <p className="text-xs font-semibold tracking-[0.18em] text-gold">SERVICE OS</p>
          <p className="mt-1 truncate font-semibold text-brand-deep">{context.tenant.name}</p>
        </div>
        <nav className="flex-1 overflow-y-auto px-3 py-4 text-sm">
          {NAV_TOP.map((item) =>
            item.migrated ? (
              <Link key={item.href} href={item.href}
                className="block rounded-lg px-3 py-2 font-medium text-ink hover:bg-brand-soft">
                {item.label}
              </Link>
            ) : (
              <a key={item.href} href={item.href}
                className="block rounded-lg px-3 py-2 text-muted hover:bg-brand-soft hover:text-ink">
                {item.label}
              </a>
            ),
          )}
          {NAV_GROUPS.map((group) => (
            <details key={group.label} className="mt-2">
              <summary className="cursor-pointer select-none rounded-lg px-3 py-2 font-semibold text-ink hover:bg-brand-soft">
                {group.label}
              </summary>
              <div className="mt-1 space-y-0.5 pl-2">
                {group.items.map((item) =>
                  item.migrated ? (
                    <Link key={item.href} href={item.href}
                      className="block rounded-lg px-3 py-1.5 text-ink hover:bg-brand-soft">
                      {item.label}
                    </Link>
                  ) : (
                    <a key={item.href} href={item.href}
                      className="block rounded-lg px-3 py-1.5 text-muted hover:bg-brand-soft hover:text-ink"
                      title="舊版後台頁面">
                      {item.label}
                    </a>
                  ),
                )}
              </div>
            </details>
          ))}
        </nav>
        <div className="border-t border-line px-5 py-3 text-xs text-muted">
          {context.user.email}
        </div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between gap-4 border-b border-line bg-surface px-5 py-3">
          <p className="truncate text-sm text-muted lg:hidden">{context.tenant.name}</p>
          <div className="ml-auto flex items-center gap-3">
            <a href="/ui/" className="rounded-lg border border-line px-3 py-1.5 text-sm text-muted hover:text-ink">
              舊版後台
            </a>
            <form action="/console/api/session/logout" method="post">
              <button className="rounded-lg border border-line px-3 py-1.5 text-sm hover:bg-danger-soft hover:text-danger">
                登出
              </button>
            </form>
          </div>
        </header>
        <main className="min-w-0 flex-1 px-5 py-6 lg:px-8">{children}</main>
      </div>
    </div>
  );
}
