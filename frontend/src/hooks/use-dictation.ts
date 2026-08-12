/**
 * Speech-to-text for interview answers, using the browser's own recogniser.
 *
 * **The privacy decision this encodes.** Every other path in this product
 * redacts direct identifiers before anything reaches a third party — that is
 * what the backend's masking layer is for. Audio cannot participate in that:
 * redaction operates on text, and the text does not exist until after
 * transcription. So dictation necessarily hands a candidate's *unredacted*
 * spoken answer to whoever performs the recognition, and in Chrome that is
 * Google's speech service rather than the local machine.
 *
 * The containment is that the leak stops there. The transcript re-enters the
 * normal path — redacted at the backend boundary before it reaches the
 * evaluator — so nothing downstream loses a guarantee it had. But this is a new
 * boundary, and it is the caller's job to say so in the interface before
 * turning the microphone on. See the disclosure in the interview page.
 *
 * **No audio is retained.** The recogniser is given the microphone stream and
 * hands back text; nothing here records, buffers or uploads a file. A recording
 * would be the most sensitive artifact this product could hold, and it needs
 * none of it once the words exist.
 *
 * Availability is honest rather than hopeful: recognition is solid in
 * Chrome/Edge, partial in Safari and absent in Firefox, so `supported` is
 * feature-detected and the caller hides the control rather than offering one
 * that will not work.
 */

"use client";

import { useCallback, useEffect, useRef, useState } from "react";

/** The slice of the Web Speech API this uses. Not in TypeScript's DOM lib. */
interface SpeechRecognitionAlternative {
  transcript: string;
}
interface SpeechRecognitionResult {
  isFinal: boolean;
  0: SpeechRecognitionAlternative;
}
interface SpeechRecognitionEvent extends Event {
  resultIndex: number;
  results: {
    length: number;
    [index: number]: SpeechRecognitionResult;
  };
}
interface SpeechRecognitionErrorEvent extends Event {
  error: string;
}
interface SpeechRecognitionLike extends EventTarget {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  start(): void;
  stop(): void;
  abort(): void;
  onresult: ((event: SpeechRecognitionEvent) => void) | null;
  onerror: ((event: SpeechRecognitionErrorEvent) => void) | null;
  onend: (() => void) | null;
}
type SpeechRecognitionConstructor = new () => SpeechRecognitionLike;

function recognitionConstructor(): SpeechRecognitionConstructor | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as {
    SpeechRecognition?: SpeechRecognitionConstructor;
    webkitSpeechRecognition?: SpeechRecognitionConstructor;
  };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

/** What went wrong, in words a candidate can act on. */
const MESSAGES: Record<string, string> = {
  "not-allowed":
    "Microphone access was blocked. Allow it in your browser settings, or type your answer instead.",
  "service-not-allowed":
    "Your browser refused speech recognition. Type your answer instead.",
  "no-speech": "Nothing was heard. Check your microphone, or type your answer instead.",
  "audio-capture":
    "No microphone was found. Plug one in, or type your answer instead.",
  network: "Speech recognition needs a connection and could not reach it.",
};

export interface Dictation {
  /** False when the browser cannot do this at all; hide the control. */
  supported: boolean;
  listening: boolean;
  /** Words recognised but not yet finalised, for a live preview. */
  interim: string;
  error: string | null;
  start: () => void;
  stop: () => void;
}

/**
 * @param onFinalText Called with each finalised phrase. Appending rather than
 *   replacing is the caller's job, so dictation can extend a typed draft rather
 *   than overwrite work already done.
 */
export function useDictation(onFinalText: (text: string) => void): Dictation {
  const [supported, setSupported] = useState(false);
  const [listening, setListening] = useState(false);
  const [interim, setInterim] = useState("");
  const [error, setError] = useState<string | null>(null);
  const recognition = useRef<SpeechRecognitionLike | null>(null);

  // Held in a ref so restarting recognition does not need a new instance every
  // render, and so the latest callback is used without re-binding handlers.
  const onFinal = useRef(onFinalText);
  useEffect(() => {
    onFinal.current = onFinalText;
  }, [onFinalText]);

  // Feature detection runs after mount, not during render: the server has no
  // `window`, and deciding this during render would make the first client paint
  // disagree with the server's HTML.
  useEffect(() => {
    setSupported(recognitionConstructor() !== null);
  }, []);

  useEffect(() => {
    return () => {
      // Abort, not stop: a component going away should drop the microphone
      // immediately rather than wait for a final result nobody will read.
      recognition.current?.abort();
      recognition.current = null;
    };
  }, []);

  const start = useCallback(() => {
    const Recognition = recognitionConstructor();
    if (!Recognition || recognition.current) return;

    setError(null);
    setInterim("");

    const instance = new Recognition();
    // Keep listening across pauses: an interview answer has them, and stopping
    // at the first silence would make this useless for anything but a sentence.
    instance.continuous = true;
    instance.interimResults = true;
    instance.lang = navigator.language || "en-US";

    instance.onresult = (event) => {
      let pending = "";
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const result = event.results[i];
        const text = result[0].transcript;
        if (result.isFinal) {
          onFinal.current(text.trim());
        } else {
          pending += text;
        }
      }
      setInterim(pending);
    };

    instance.onerror = (event) => {
      // Released here as well as in `onend`, and that is the fix for the second
      // half of "it said blocked, and then it still did nothing after I allowed
      // it". `start()` returns early while this ref is set, so a recogniser that
      // errors without firing `onend` -- which some implementations do -- leaves
      // the button permanently dead. Clearing it in both places means the next
      // press always gets a fresh instance.
      recognition.current = null;
      setListening(false);

      // "aborted" is what a deliberate stop looks like; it is not a failure and
      // must not be shown as one.
      if (event.error === "aborted") return;
      setError(MESSAGES[event.error] ?? "Speech recognition stopped unexpectedly.");
    };

    instance.onend = () => {
      setListening(false);
      setInterim("");
      recognition.current = null;
    };

    try {
      instance.start();
      recognition.current = instance;
      setListening(true);
    } catch {
      // Chrome throws if start() is called while an instance is already live.
      setError("Speech recognition is already running.");
    }
  }, []);

  const stop = useCallback(() => {
    recognition.current?.stop();
  }, []);

  return { supported, listening, interim, error, start, stop };
}
