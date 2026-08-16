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
       * gave **19%**, which was the true figure at the time.
       *
       * It is **32.8%** now, and the way it rose is the point: `bff.ts`,
       * `question-clock.ts`, `use-dictation.ts` and `use-speech.ts` gained real
       * tests. Under the default setting that work would have *lowered* the
       * reported number, because each newly-imported file drags its untested
       * siblings into the denominator with it.
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
       * This catches one thing: tests being deleted or silently stopping
       * running. It does not mean the frontend is tested -- the pages and the
       * UI components still have no tests at all, and the honest fix for that
       * is tests, not a number in a config file. Raise these as they arrive,
       * which is what happened here: they were 15/12/15/15 against a measured
       * 19%, and the hooks and lib tests that landed since moved it to 32.8%.
       */
      thresholds: {
        // Each set just below its measured value (32.8 / 23.1 / 28.2 / 32.6).
        // Branches is the lowest because the untested UI components are almost
        // all branch-free, so they add to the denominator without adding
        // branches anyone could cover.
        statements: 30,
        branches: 21,
        functions: 26,
        lines: 30,
      },
    },
  },
});
