import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";

export default defineConfig({
  root: "web",
  plugins: [react()],
  build: {
    outDir: "out",
    emptyOutDir: true,
    assetsDir: "static",
    sourcemap: false,
    rollupOptions: {
      input: {
        index: resolve(import.meta.dirname, "web/index.html"),
        sources: resolve(import.meta.dirname, "web/sources.html"),
        watch: resolve(import.meta.dirname, "web/watch.html"),
      },
    },
  },
});
