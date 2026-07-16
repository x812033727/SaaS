import { redirect } from "next/navigation";

import AppShell from "@/components/shell/AppShell";
import Providers from "@/app/providers";
import { apiFetch, type AppContext } from "@/lib/api";

/** 已登入區共用 layout:取 context、掛外殼與 react-query provider。 */
export default async function AppLayout({ children }: { children: React.ReactNode }) {
  let context: AppContext;
  try {
    context = await apiFetch<AppContext>("/api/v1/context");
  } catch (error) {
    if (error instanceof Error && error.message === "UNAUTHENTICATED") redirect("/login");
    throw error;
  }
  return (
    <Providers>
      <AppShell context={context}>{children}</AppShell>
    </Providers>
  );
}
