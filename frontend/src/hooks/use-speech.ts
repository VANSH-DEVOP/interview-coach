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

export function useSpeech(): Speech {
  const [supported, setSupported] = useState(false);
  const [speaking, setSpeaking] = useState(false);

  // After mount, not during render: the server has no `window`, and deciding
  // during render would make the first client paint disagree with the HTML.
  useEffect(() => {
    setSupported(typeof window !== "undefined" && "speechSynthesis" in window);
  }, []);

  useEffect(() => {
    return () => {
      // Leaving the page must stop the voice. Otherwise it keeps reading a
      // question that is no longer on screen, which is genuinely alarming.
      if (typeof window !== "undefined" && "speechSynthesis" in window) {
        window.speechSynthesis.cancel();
      }
    };
  }, []);

  const speak = useCallback((text: string) => {
    if (typeof window === "undefined" || !("speechSynthesis" in window)) return;
    // Cancel first: queueing is the default, so a second press without this
    // reads both questions back to back instead of switching to the new one.
    window.speechSynthesis.cancel();

    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = navigator.language || "en-US";
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
