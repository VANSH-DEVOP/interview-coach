import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      // Mirrors the "@/*" path mapping in tsconfig.json.
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  test: {
    // jsdom, because the API client reads and writes document.cookie and the
    // components under test render.
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    coverage: {
      provider: "v8",
      reporter: ["text", "lcov"],
      /**
       * `all: true` is the whole point of this block, and it is the difference
       * between a real number and a flattering one.
       *
       * v8 reports only the files a test imported. Left at the default, this
       * project scored **86%** -- because that was 86% of `api-client.ts`,
       * `use-auth.ts` and `middleware.ts`, the three files with tests, and every
       * page and component was simply absent from the denominator. Counting them
       * gives **19%**, which is the true figure.
       *
       * The flattering number is worse than none: it would also *fall* whenever
       * someone added the first test for a page, because that page would join
       * the denominator mostly uncovered. A metric that punishes writing a test
       * is not a metric worth having.
       */
      all: true,
      include: ["src/**/*.{ts,tsx}"],
      exclude: [
        "src/**/*.test.{ts,tsx}",
        "src/test/**",
        // Type declarations only; no statements to execute.
        "src/types/**",
      ],
      /**
       * A floor, not a target, and deliberately far below the backend's 90.
       *
       * At 19% this catches one thing: tests being deleted or silently stopping
       * running. It does not mean the frontend is tested -- the pages and the UI
       * components have no tests at all, and the honest fix for that is tests,
       * not a number in a config file. Raise this as they arrive.
       */
      thresholds: {
        // Each set just below its measured value (19.4 / 14.5 / 19.3 / 18.5).
        // Branches is the lowest because the untested UI components are almost
        // all branch-free, so they add to the denominator without adding
        // branches anyone could cover.
        statements: 15,
        branches: 12,
        functions: 15,
        lines: 15,
      },
    },
  },
});
