import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
export default defineConfig({
  plugins: [react()],
  server: { fs: { allow: [".."] } },
  test: { include: ["tests/**/*.test.ts"] },
});
