"use client";

/**
 * When the candidate first saw a given question, surviving a reload.
 *
 * The clock used to start at `Date.now()` every time the component mounted, so
 * refreshing the page mid-question silently restarted it and the reported
 * duration described the time since the reload rather than the time spent. That
 * number is no longer cosmetic: it reaches the evaluator, which is told it is
 * "the whole time from seeing the question to submitting" and comments on
 * pacing from it.
 *
 * `sessionStorage`, not `localStorage`: this is per-tab working state that
 * should not outlive the tab, and two tabs on the same interview are two
 * separate attempts rather than one shared clock.
 *
 * Storage can throw -- Safari in private mode, a full quota -- and a timer is
 * never worth failing a page for, so every access degrades to the in-memory
 * behaviour that existed before.
 */
export function questionClockKey(sessionId: string, questionId: string): string {
  return `ip:question-started:${sessionId}:${questionId}`;
}

export function readOrStartClock(sessionId: string, questionId: string): number {
  const now = Date.now();
  if (typeof window === "undefined") return now;
  const key = questionClockKey(sessionId, questionId);
  try {
    const stored = window.sessionStorage.getItem(key);
    if (stored) {
      const parsed = Number(stored);
      // A stored value in the future is a clock change or a tampered entry;
      // starting again beats reporting a negative duration.
      if (Number.isFinite(parsed) && parsed <= now) return parsed;
    }
    window.sessionStorage.setItem(key, String(now));
  } catch {
    /* storage unavailable: fall back to this render's start */
  }
  return now;
}

export function clearClock(sessionId: string, questionId: string): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(questionClockKey(sessionId, questionId));
  } catch {
    /* nothing to do */
  }
}
