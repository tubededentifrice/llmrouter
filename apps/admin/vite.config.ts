import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    allowedHosts: ["llmrouter.opendle.dev", "llmrouter.opendle.com"],
  },
});
