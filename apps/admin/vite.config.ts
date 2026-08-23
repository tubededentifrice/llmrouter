import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  optimizeDeps: { exclude: ["@opendle/ui"] },
  server: {
    allowedHosts: ["llmrouter.opendle.dev", "llmrouter.opendle.com"],
    proxy: localProxy(),
    headers: securityHeaders(true),
  },
  preview: { headers: securityHeaders(false) },
});

function securityHeaders(development: boolean) {
  const inline = development ? " 'unsafe-inline'" : "";
  return {
    "Cache-Control": "no-store",
    "Content-Security-Policy": `default-src 'self'; script-src 'self'${inline}; style-src 'self'${inline}; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'`,
    "X-Content-Type-Options": "nosniff",
  };
}

function localProxy() {
  const target = process.env.LLMROUTER_VITE_PROXY_ORIGIN;
  if (target === undefined) return {};
  const url = new URL(target);
  if (
    url.origin !== target ||
    url.protocol !== "http:" ||
    url.hostname !== "backend"
  )
    throw new Error("LLMROUTER_VITE_PROXY_ORIGIN is invalid.");
  return { "/v1": { target, changeOrigin: false } };
}
