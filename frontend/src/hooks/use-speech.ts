/**
 * Reading a question aloud, using the browser's own synthesiser.
 *
 * Unlike dictation, **this sends nothing anywhere.** `speechSynthesis` renders
 * locally from voices already installed on the device, so there is no third
 * party and no new boundary — the question text never leaves the browser it was
 * already displayed in.
 *
 * It is a play button, not a mode. The written question stays on screen and
 * stays the source of truth: audio that could *replace* the text would break
 * skimming, re-reading, and anyone who cannot play sound.
 */

"use client";

import { useCallback, useEffect, useState } from "react";

export interface Speech {
  /** False when the browser cannot speak; hide the control. */
  supported: boolean;
  speaking: boolean;
  speak: (text: string) => void;
  stop: () => void;
}


/**
 * Voice names that are actually pleasant to listen to, best first.
 *
 * The platform default is whatever the OS picked years ago -- on macOS a
 * formant synthesiser from the 1990s. Every name here is a modern neural voice
 * shipped by one of the major platforms; the list is a preference, not a
 * requirement, and an unknown system falls through to the first local
 * language-matched voice and then to the browser's own default.
 */
const PREFERRED_VOICES = [
  "Google US English",
  "Microsoft Aria Online (Natural) - English (United States)",
  "Microsoft Jenny Online (Natural) - English (United States)",
  "Samantha",
  "Karen",
  "Daniel",
];

function preferredVoice(): SpeechSynthesisVoice | null {
  // getVoices() is populated asynchronously and returns [] on the very first
  // call in some browsers. Returning null then is correct -- the utterance
  // simply uses the default that time, and the next press has the list.
  const voices = window.speechSynthesis.getVoices();
  if (voices.length === 0) return null;

  for (const name of PREFERRED_VOICES) {
    const match = voices.find((voice) => voice.name === name);
    if (match) return match;
  }

  const language = navigator.language || "en-US";
  return (
    voices.find((voice) => voice.lang === language && voice.localService) ??
    voices.find((voice) => voice.lang.startsWith(language.split("-")[0])) ??
    null
  );
}

export function useSpeech(): Speech {
  const [supported, setSupported] = useState(false);
  const [speaking, setSpeaking] = useState(false);

  // After mount, not during render: the server has no `window`, and deciding
  // during render would make the first client paint disagree with the HTML.
  useEffect(() => {
    const available = typeof window !== "undefined" && "speechSynthesis" in window;
    setSupported(available);
    if (!available) return;
    // Touch the list once on mount so it is populated before the first press.
    // Chrome fills it asynchronously and fires this event when it does; without
    // the warm-up the first question is read in the default voice and every
    // one after it in the chosen one, which looks like a bug.
    // Captured, not re-read in the cleanup: the cleanup runs during teardown,
    // by which point `window.speechSynthesis` may already be gone, and a
    // throwing cleanup takes the unmount with it.
    const synthesis = window.speechSynthesis;
    synthesis.getVoices();
    const onVoices = () => synthesis.getVoices();
    synthesis.addEventListener?.("voiceschanged", onVoices);
    return () => {
      synthesis.removeEventListener?.("voiceschanged", onVoices);
    };
  }, []);

  useEffect(() => {
    if (typeof window === "undefined" || !("speechSynthesis" in window)) return;
    const synthesis = window.speechSynthesis;
    return () => {
      // Leaving the page must stop the voice. Otherwise it keeps reading a
      // question that is no longer on screen, which is genuinely alarming.
      synthesis.cancel();
    };
  }, []);

  const speak = useCallback((text: string) => {
    if (typeof window === "undefined" || !("speechSynthesis" in window)) return;
    // Cancel first: queueing is the default, so a second press without this
    // reads both questions back to back instead of switching to the new one.
    window.speechSynthesis.cancel();

    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = navigator.language || "en-US";

    // Left to the platform default this was described as "a 90-year-old asking
    // the question, very quietly", which is fair: the default voice on macOS is
    // an old formant synthesiser, the default volume is not 1, and the default
    // rate reads as a drawl on long sentences. All three are set explicitly.
    utterance.volume = 1;
    utterance.rate = 1.05;
    utterance.pitch = 1;

    const voice = preferredVoice();
    if (voice) utterance.voice = voice;

    utterance.onend = () => setSpeaking(false);
    // `onerror` fires for an interruption as well as a fault, and both mean
    // the same thing here: nothing is being spoken any more.
    utterance.onerror = () => setSpeaking(false);

    setSpeaking(true);
    window.speechSynthesis.speak(utterance);
  }, []);

  const stop = useCallback(() => {
    if (typeof window === "undefined" || !("speechSynthesis" in window)) return;
    window.speechSynthesis.cancel();
    setSpeaking(false);
  }, []);

  return { supported, speaking, speak, stop };
}
