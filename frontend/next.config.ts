import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  reactStrictMode: true,
  // console 全站掛在 /console 底下:FastAPI 佔用的頂層前綴太多(/ui /auth /api
  // /booking /payments /line …),nginx 只需一條 `location ^~ /console` 反代本服務,
  // `location /` 維持指向 FastAPI 不動。全站遷移完成後再拿掉並對調 nginx 預設。
  basePath: "/console",
};

export default nextConfig;
