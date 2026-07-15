import { cookies } from "next/headers";
import { redirect } from "next/navigation";

export default async function Home() {
  const authenticated = Boolean((await cookies()).get("saas_access_token"));
  redirect(authenticated ? "/dashboard" : "/login");
}
