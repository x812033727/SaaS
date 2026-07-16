import Link from "next/link";

import MobileNavReset from "@/components/shell/MobileNavReset";
import { NAV_GROUPS, NAV_TOP } from "@/lib/nav";
import type { AppContext } from "@/lib/api";

/** 後台外殼:精品風側欄(同構 /ui 分組)+ 頂欄。Server Component;
 * 手機版抽屜為 checkbox+peer 純 CSS(仿 /ui nav-toggle),換頁收合由
 * MobileNavReset 處理。 */
export default function AppShell({
  context,
  children,
}: {
  context: AppContext;
  children: React.ReactNode;
}) {
  return (
    <div className="flex min-h-screen">
      <input type="checkbox" id="nav-toggle" className="peer sr-only" aria-hidden="true" />
      <MobileNavReset />
      {/* 手機 scrim:抽屜開啟時罩住內容,點擊即關 */}
      <label
        htmlFor="nav-toggle"
        className="fixed inset-0 z-30 hidden bg-black/30 peer-checked:block lg:hidden"
        aria-hidden="true"
      />
      <aside className="fixed inset-y-0 left-0 z-40 flex w-60 shrink-0 -translate-x-full flex-col border-r border-line bg-surface transition-transform duration-200 peer-checked:translate-x-0 lg:static lg:translate-x-0">
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
          <label
            htmlFor="nav-toggle"
            className="flex cursor-pointer flex-col gap-1 rounded-md border border-line p-2 lg:hidden"
            aria-label="切換選單"
          >
            <span className="h-0.5 w-5 bg-ink" />
            <span className="h-0.5 w-5 bg-ink" />
            <span className="h-0.5 w-5 bg-ink" />
          </label>
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
