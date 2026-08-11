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
  onend: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public text: string) {
    FakeUtterance.last = this;
  }
}

function install() {
  const synth = { speak: vi.fn(), cancel: vi.fn() };
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
