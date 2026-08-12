/**
 * The per-question clock.
 *
 * This used to be `Date.now()` taken on mount, so refreshing the page
 * mid-question silently restarted it and the reported duration described the
 * time since the reload. The number stopped being cosmetic when pacing feedback
 * landed: it reaches the evaluator, which is told it is "the whole time from
 * seeing the question to submitting" and comments on it.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { clearClock, readOrStartClock } from "./question-clock";

const SESSION = "session-1";
const QUESTION = "question-1";

beforeEach(() => {
  window.sessionStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
  window.sessionStorage.clear();
});

describe("surviving a reload", () => {
  it("returns the same start the second time it is asked", () => {
    const first = readOrStartClock(SESSION, QUESTION);

    // A reload is exactly this: the component mounts again and asks again.
    const second = readOrStartClock(SESSION, QUESTION);

    expect(second).toBe(first);
  });

  it("keeps a separate clock per question", () => {
    const one = readOrStartClock(SESSION, "q1");
    vi.setSystemTime(new Date(Date.now() + 60_000));
    const two = readOrStartClock(SESSION, "q2");

    expect(two).not.toBe(one);
    // And the first is untouched by the second.
    expect(readOrStartClock(SESSION, "q1")).toBe(one);
    vi.useRealTimers();
  });

  it("keeps a separate clock per interview", () => {
    const mine = readOrStartClock("session-a", QUESTION);
    window.sessionStorage.removeItem(`ip:question-started:session-a:${QUESTION}`);
    const theirs = readOrStartClock("session-b", QUESTION);

    expect(window.sessionStorage.getItem(`ip:question-started:session-b:${QUESTION}`)).toBe(
      String(theirs),
    );
    expect(mine).toBeTypeOf("number");
  });

  it("starts again after the answer is submitted", () => {
    const first = readOrStartClock(SESSION, QUESTION);
    clearClock(SESSION, QUESTION);

    vi.setSystemTime(new Date(first + 5_000));
    const second = readOrStartClock(SESSION, QUESTION);

    // Otherwise "Change answer" would resume a start from before the first
    // attempt and report a duration covering both.
    expect(second).toBeGreaterThan(first);
    vi.useRealTimers();
  });
});

describe("when the stored value cannot be trusted", () => {
  it("ignores a start in the future", () => {
    // A clock change, or a tampered entry. Reporting a negative duration would
    // be worse than starting again.
    window.sessionStorage.setItem(
      `ip:question-started:${SESSION}:${QUESTION}`,
      String(Date.now() + 60_000),
    );

    const started = readOrStartClock(SESSION, QUESTION);

    expect(started).toBeLessThanOrEqual(Date.now());
  });

  it("ignores a value that is not a number", () => {
    window.sessionStorage.setItem(`ip:question-started:${SESSION}:${QUESTION}`, "soon");

    const started = readOrStartClock(SESSION, QUESTION);

    expect(Number.isFinite(started)).toBe(true);
  });
});

describe("when storage is unavailable", () => {
  it("still returns a usable start", () => {
    // Safari in private mode, or a full quota. A timer is never worth failing a
    // page for, so this degrades to the in-memory behaviour it replaced.
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("quota");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });

    const started = readOrStartClock(SESSION, QUESTION);

    expect(started).toBeLessThanOrEqual(Date.now());
    expect(Number.isFinite(started)).toBe(true);
  });

  it("does not throw when clearing", () => {
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new Error("quota");
    });

    expect(() => clearClock(SESSION, QUESTION)).not.toThrow();
  });
});
