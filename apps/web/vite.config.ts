import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 개발 시 API 는 같은 출처처럼 프록시한다(쿠키 SameSite=Strict 유지)
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { "/api": "http://127.0.0.1:8765" } },
  build: { outDir: "dist", sourcemap: false },
});
