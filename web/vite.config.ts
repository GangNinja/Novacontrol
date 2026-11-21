import { defineConfig, type ProxyOptions } from "vite";
import react from "@vitejs/plugin-react";

// The local Vite proxy is the trusted local operator surface.  It supplies the
// loopback-only admin credential so a new browser tab never has to prompt for
// the API token.  The backend accepts this credential only from 127.0.0.1.
const adminToken = process.env.NOVACONTROL_ADMIN_TOKEN || "narasimha-novacontrol";
const addAdminAuth: ProxyOptions["configure"] = (proxy) => {
  proxy.on("proxyReq", (proxyReq, req) => {
    // A token entered in the UI still wins; the proxy only fills the gap.
    if (!req.headers.authorization) {
      proxyReq.setHeader("Authorization", `Bearer ${adminToken}`);
    }
    if (!req.headers["x-autonomy-operator-token"]) {
      proxyReq.setHeader("X-Autonomy-Operator-Token", adminToken);
    }
  });
};

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/market": {
        target: process.env.NOVAMARKET_API_ORIGIN || "http://127.0.0.1:8011",
        changeOrigin: true,
        configure: addAdminAuth,
      },
      "/events": {
        target: process.env.NOVAMARKET_API_ORIGIN || "http://127.0.0.1:8011",
        changeOrigin: true,
        configure: addAdminAuth,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    target: "es2022",
  },
});
