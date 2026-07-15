import "server-only";

import { cookies } from "next/headers";

export type AppContext = {
  user: { id: number; email: string };
  organization: {
    id: number;
    name: string;
    slug: string;
    role: string;
    share_customers: boolean;
    share_loyalty: boolean;
    share_coupons: boolean;
  };
  tenant: { id: number; name: string; role: string };
  permissions: string[];
};

const apiOrigin = process.env.SAAS_API_INTERNAL_URL ?? "http://127.0.0.1:8000";

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = (await cookies()).get("saas_access_token")?.value;
  if (!token) throw new Error("UNAUTHENTICATED");
  const response = await fetch(`${apiOrigin}${path}`, {
    ...init,
    cache: "no-store",
    headers: {
      Accept: "application/json",
      Authorization: `Bearer ${token}`,
      ...init.headers,
    },
  });
  if (response.status === 401) throw new Error("UNAUTHENTICATED");
  if (!response.ok) throw new Error(`API_${response.status}`);
  return response.json() as Promise<T>;
}
