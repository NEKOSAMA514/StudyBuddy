import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { codeInspectorPlugin } from "code-inspector-plugin";

export default defineConfig({
  plugins: [
    react(),
    codeInspectorPlugin({
      bundler: "vite",
    }),
  ],
  build: { manifest: true },
  server: {
    proxy: {
      "/api": process.env.E2E_API_PROXY || "http://127.0.0.1:18000",
      "/__test__": process.env.E2E_API_PROXY || "http://127.0.0.1:18000",
    },
  },
});
