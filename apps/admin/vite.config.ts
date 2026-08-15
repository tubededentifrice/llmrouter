import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  optimizeDeps: {
    exclude: ["@opendle/ui"],
  },
  server: {
    allowedHosts: ["llmrouter.opendle.dev", "llmrouter.opendle.com"],
  },
});
