import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

export default defineConfig({
  plugins: [react(), frameSecurityHeaders()],
  optimizeDeps: { exclude: ["@opendle/ui"] },
  server: {
    allowedHosts: ["llmrouter.opendle.dev", "llmrouter.opendle.com"],
    proxy: localProxy(),
    headers: {
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
    },
  },
  preview: {
    headers: {
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
    },
  },
});

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

function frameSecurityHeaders(): Plugin {
  return {
    name: "llmrouter-frame-security-headers",
    configureServer(server) {
      server.middlewares.use((request, response, next) => {
        setFramePolicy(request.url, true, (name, value) => {
          response.setHeader(name, value);
        });
        next();
      });
    },
    configurePreviewServer(server) {
      server.middlewares.use((request, response, next) => {
        setFramePolicy(request.url, false, (name, value) => {
          response.setHeader(name, value);
        });
        next();
      });
    },
  };
}

function setFramePolicy(
  requestUrl: string | undefined,
  development: boolean,
  setHeader: (name: string, value: string) => void,
) {
  setHeader(
    "Content-Security-Policy",
    framePolicyForUrl(requestUrl, development),
  );
}

export function framePolicyForUrl(
  requestUrl: string | undefined,
  development = false,
): string {
  const url = new URL(requestUrl ?? "/", "http://127.0.0.1");
  let ancestors = "'self'";
  if (url.pathname === "/service-administration") {
    ancestors = "'none'";
    const values = url.searchParams.getAll("host_origin");
    if (values.length === 1 && exactOrigin(values[0] ?? ""))
      ancestors = values[0] ?? "'none'";
  }
  const inline = development ? " 'unsafe-inline'" : "";
  return `default-src 'self'; script-src 'self'${inline}; style-src 'self'${inline}; img-src 'self' data:; connect-src 'self'; frame-ancestors ${ancestors}; base-uri 'none'; form-action 'self'; object-src 'none'`;
}

function exactOrigin(value: string): boolean {
  try {
    const url = new URL(value);
    const loopback = ["127.0.0.1", "localhost", "::1"].includes(url.hostname);
    return (
      url.origin === value &&
      (url.protocol === "https:" || (url.protocol === "http:" && loopback))
    );
  } catch {
    return false;
  }
}
