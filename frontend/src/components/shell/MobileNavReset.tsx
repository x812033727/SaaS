"use client";

import { usePathname } from "next/navigation";
import { useEffect } from "react";

/** Next 換頁不重載頁面,checkbox 抽屜不會自動收合 — 路徑變更時取消勾選。 */
export default function MobileNavReset() {
  const pathname = usePathname();
  useEffect(() => {
    const toggle = document.getElementById("nav-toggle") as HTMLInputElement | null;
    if (toggle) toggle.checked = false;
  }, [pathname]);
  return null;
}
