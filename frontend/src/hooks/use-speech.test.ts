/**
 * Reading questions aloud. Unlike dictation this sends nothing anywhere — the
 * synthesiser renders locally — so the behaviours worth pinning are the ones
 * that would leave a voice talking about the wrong thing.
 */

import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useSpeech } from "./use-speech";

class FakeUtterance {
  static last: FakeUtterance | null = null;
  lang = "";
  volume = 0.5;
  rate = 0.5;
  pitch = 0.5;
  voice: unknown = null;
  onend: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public text: string) {
    FakeUtterance.last = this;
  }
}

/** Enough of a voice list to exercise the preference order. */
const VOICES = [
  { name: "Albert", lang: "en-US", localService: true },
  { name: "Samantha", lang: "en-US", localService: true },
];

function install(voices: unknown[] = VOICES) {
  const synth = {
    speak: vi.fn(),
    cancel: vi.fn(),
    getVoices: vi.fn(() => voices),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  };
  (window as unknown as Record<string, unknown>).speechSynthesis = synth;
  (window as unknown as Record<string, unknown>).SpeechSynthesisUtterance =
    FakeUtterance;
  return synth;
}

afterEach(() => {
  delete (window as unknown as Record<string, unknown>).speechSynthesis;
  delete (window as unknown as Record<string, unknown>).SpeechSynthesisUtterance;
  FakeUtterance.last = null;
});

it("reports unsupported when the browser cannot speak", async () => {
  const { result } = renderHook(() => useSpeech());

  await waitFor(() => expect(result.current.supported).toBe(false));
});

it("speaks the text it is given", async () => {
  const synth = install();
  const { result } = renderHook(() => useSpeech());
  await waitFor(() => expect(result.current.supported).toBe(true));

  act(() => result.current.speak("Tell me about Kafka."));

  expect(synth.speak).toHaveBeenCalledTimes(1);
  expect(FakeUtterance.last!.text).toBe("Tell me about Kafka.");
  expect(result.current.speaking).toBe(true);
});

it("cancels before speaking so a second press switches rather than queues", async () => {
  const synth = install();
  const { result } = renderHook(() => useSpeech());
  await waitFor(() => expect(result.current.supported).toBe(true));

  act(() => result.current.speak("First question."));
  act(() => result.current.speak("Second question."));

  // Queueing is the platform default, so without the cancel this would read
  // both questions back to back instead of the one on screen.
  expect(synth.cancel).toHaveBeenCalledTimes(2);
  expect(FakeUtterance.last!.text).toBe("Second question.");
});

it("stops considering itself speaking when the utterance ends", async () => {
  install();
  const { result } = renderHook(() => useSpeech());
  await waitFor(() => expect(result.current.supported).toBe(true));
  act(() => result.current.speak("A question."));

  act(() => FakeUtterance.last!.onend?.());

  expect(result.current.speaking).toBe(false);
});

it("treats an interruption the same as an end", async () => {
  install();
  const { result } = renderHook(() => useSpeech());
  await waitFor(() => expect(result.current.supported).toBe(true));
  act(() => result.current.speak("A question."));

  // `onerror` fires for an interruption as well as a fault, and both mean the
  // same thing: nothing is being spoken any more.
  act(() => FakeUtterance.last!.onerror?.());

  expect(result.current.speaking).toBe(false);
});

it("silences the voice when the component goes away", async () => {
  const synth = install();
  const { result, unmount } = renderHook(() => useSpeech());
  await waitFor(() => expect(result.current.supported).toBe(true));
  act(() => result.current.speak("A question."));

  unmount();

  // Otherwise it keeps reading a question that is no longer on screen.
  expect(synth.cancel).toHaveBeenCalled();
});


describe("how it sounds", () => {
  it("sets volume, rate and pitch rather than accepting the defaults", async () => {
    // Reported as "a 90-year-old asking the question, very quietly". The
    // platform defaults are not 1, and on long sentences the default rate
    // reads as a drawl.
    install();
    const { result } = renderHook(() => useSpeech());
    await waitFor(() => expect(result.current.supported).toBe(true));

    act(() => result.current.speak("Tell me about a system you designed."));

    expect(FakeUtterance.last!.volume).toBe(1);
    expect(FakeUtterance.last!.rate).toBeGreaterThan(1);
    expect(FakeUtterance.last!.pitch).toBe(1);
  });

  it("prefers a modern voice over whatever the platform picked", async () => {
    // "Albert" is first in the list the platform returns and is exactly the
    // 1990s formant synthesiser being avoided.
    install();
    const { result } = renderHook(() => useSpeech());
    await waitFor(() => expect(result.current.supported).toBe(true));

    act(() => result.current.speak("A question."));

    expect((FakeUtterance.last!.voice as { name: string }).name).toBe("Samantha");
  });

  it("falls back to a language match when no preferred voice exists", async () => {
    install([{ name: "Unknown Voice", lang: "en-US", localService: true }]);
    const { result } = renderHook(() => useSpeech());
    await waitFor(() => expect(result.current.supported).toBe(true));

    act(() => result.current.speak("A question."));

    expect((FakeUtterance.last!.voice as { name: string }).name).toBe("Unknown Voice");
  });

  it("speaks anyway when the voice list is not populated yet", async () => {
    // getVoices() returns [] on the first call in some browsers. The utterance
    // should still be spoken, in the default voice, rather than dropped.
    const synth = install([]);
    const { result } = renderHook(() => useSpeech());
    await waitFor(() => expect(result.current.supported).toBe(true));

    act(() => result.current.speak("A question."));

    expect(synth.speak).toHaveBeenCalledTimes(1);
    expect(FakeUtterance.last!.voice).toBeNull();
  });
});
