import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
  build: {
    // 朱雀仿宋按 unicode-range 切片保持独立 .woff2 文件按需加载，不把小分片内联成 data: URI
    assetsInlineLimit: (filePath) =>
      filePath.replace(/\\/g, "/").includes("/zhuque-fangsong/")
        ? false
        : undefined,
  },
  server: {
    port: 3000,
    proxy: {
      "/api": {
        target: process.env.VITE_API_PROXY_TARGET || "http://127.0.0.1:6666",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
