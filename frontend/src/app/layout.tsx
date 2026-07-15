import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "服務營運平台", template: "%s｜服務營運平台" },
  description: "LINE 原生預約、顧客與營運管理平台",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-Hant">
      <body>{children}</body>
    </html>
  );
}
