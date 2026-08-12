/**
 * Dictation, tested against a fake Web Speech recogniser.
 *
 * The behaviours worth pinning are the ones that would silently ruin an answer
 * rather than throw: appending instead of replacing, interim results never
 * being committed, and a deliberate stop not being reported as an error.
 */

import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useDictation } from "./use-dictation";

/** Stands in for SpeechRecognition, driven by the test. */
class FakeRecognition {
  static last: FakeRecognition | null = null;
  continuous = false;
  interimResults = false;
  lang = "";
  started = false;
  aborted = false;
  onresult: ((event: unknown) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  onend: (() => void) | null = null;

  constructor() {
    FakeRecognition.last = this;
  }

  start() {
    this.started = true;
  }

  stop() {
    this.onend?.();
  }

  abort() {
    this.aborted = true;
  }

  /** Emit results the way the browser does, as an indexed list. */
  emit(items: Array<{ text: string; final: boolean }>) {
    const results = items.map((item) => ({
      isFinal: item.final,
      0: { transcript: item.text },
    }));
    this.onresult?.({ resultIndex: 0, results: { ...results, length: results.length } });
  }
}

function install() {
  (window as unknown as Record<string, unknown>).SpeechRecognition = FakeRecognition;
}

afterEach(() => {
  delete (window as unknown as Record<string, unknown>).SpeechRecognition;
  delete (window as unknown as Record<string, unknown>).webkitSpeechRecognition;
  FakeRecognition.last = null;
});

describe("availability", () => {
  it("reports unsupported when the browser has no recogniser", async () => {
    const { result } = renderHook(() => useDictation(vi.fn()));

    // Firefox has none at all, so the caller hides the control rather than
    // offering one that cannot work.
    await waitFor(() => expect(result.current.supported).toBe(false));
  });

  it("reports supported when it does", async () => {
    install();
    const { result } = renderHook(() => useDictation(vi.fn()));

    await waitFor(() => expect(result.current.supported).toBe(true));
  });

  it("accepts the webkit-prefixed name", async () => {
    (window as unknown as Record<string, unknown>).webkitSpeechRecognition =
      FakeRecognition;
    const { result } = renderHook(() => useDictation(vi.fn()));

    await waitFor(() => expect(result.current.supported).toBe(true));
  });
});

describe("transcribing", () => {
  it("hands over only finalised phrases", async () => {
    install();
    const onFinal = vi.fn();
    const { result } = renderHook(() => useDictation(onFinal));
    await waitFor(() => expect(result.current.supported).toBe(true));

    act(() => result.current.start());
    act(() =>
      FakeRecognition.last!.emit([
        { text: "I built a payments", final: true },
        { text: " ledger and", final: false },
      ]),
    );

    // Interim text is a preview only: committing it would duplicate words when
    // the recogniser revises them a moment later.
    expect(onFinal).toHaveBeenCalledTimes(1);
    expect(onFinal).toHaveBeenCalledWith("I built a payments");
    expect(result.current.interim).toBe(" ledger and");
  });

  it("keeps listening across pauses", async () => {
    install();
    const { result } = renderHook(() => useDictation(vi.fn()));
    await waitFor(() => expect(result.current.supported).toBe(true));

    act(() => result.current.start());

    // An interview answer has pauses in it; stopping at the first silence
    // would make this useless for anything longer than a sentence.
    expect(FakeRecognition.last!.continuous).toBe(true);
    expect(FakeRecognition.last!.interimResults).toBe(true);
  });

  it("clears the interim preview when recognition ends", async () => {
    install();
    const { result } = renderHook(() => useDictation(vi.fn()));
    await waitFor(() => expect(result.current.supported).toBe(true));

    act(() => result.current.start());
    act(() => FakeRecognition.last!.emit([{ text: "half a thought", final: false }]));
    expect(result.current.interim).not.toBe("");

    act(() => result.current.stop());

    // Otherwise a half-heard phrase would sit under the textarea for ever.
    expect(result.current.interim).toBe("");
    expect(result.current.listening).toBe(false);
  });
});

describe("failures", () => {
  it("explains a blocked microphone in words a candidate can act on", async () => {
    install();
    const { result } = renderHook(() => useDictation(vi.fn()));
    await waitFor(() => expect(result.current.supported).toBe(true));

    act(() => result.current.start());
    act(() => FakeRecognition.last!.onerror?.({ error: "not-allowed" }));

    expect(result.current.error).toMatch(/Microphone access was blocked/);
    expect(result.current.error).toMatch(/type your answer instead/);
  });

  it("does not report a deliberate stop as an error", async () => {
    install();
    const { result } = renderHook(() => useDictation(vi.fn()));
    await waitFor(() => expect(result.current.supported).toBe(true));

    act(() => result.current.start());
    // "aborted" is what stopping looks like from the inside.
    act(() => FakeRecognition.last!.onerror?.({ error: "aborted" }));

    expect(result.current.error).toBeNull();
  });

  it("drops the microphone when the component goes away", async () => {
    install();
    const { result, unmount } = renderHook(() => useDictation(vi.fn()));
    await waitFor(() => expect(result.current.supported).toBe(true));
    act(() => result.current.start());

    unmount();

    // Abort rather than stop: leaving a page should release the microphone at
    // once, not wait for a final result nobody will read.
    expect(FakeRecognition.last!.aborted).toBe(true);
  });
});

describe("recovering from a refusal", () => {
  it("can be started again after an error", async () => {
    // The second half of the reported failure: the microphone was blocked by a
    // Permissions-Policy header, and after the user allowed it in site settings
    // the button still did nothing. `start()` returns early while an instance
    // is held, so an error that did not also fire `onend` left the ref set and
    // the control dead for the rest of the page's life.
    install();
    const { result } = renderHook(() => useDictation(vi.fn()));
    await waitFor(() => expect(result.current.supported).toBe(true));

    act(() => result.current.start());
    const first = FakeRecognition.last;
    act(() => FakeRecognition.last!.onerror?.({ error: "not-allowed" }));
    expect(result.current.error).toMatch(/blocked/i);

    act(() => result.current.start());

    expect(FakeRecognition.last).not.toBe(first);
    expect(result.current.listening).toBe(true);
    // And the stale message is gone, rather than sitting under a live mic.
    expect(result.current.error).toBeNull();
  });
});
