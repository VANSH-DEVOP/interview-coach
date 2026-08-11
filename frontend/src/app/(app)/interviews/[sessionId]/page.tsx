"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import {
  CheckCircle2,
  ChevronRight,
  Pencil,
  SkipForward,
  Timer,
  Trash2,
  Wand2,
} from "lucide-react";

import { api, ApiError } from "@/lib/api-client";
import type { Answer, InterviewSessionDetail, Question } from "@/types";
import { PageHeader } from "@/components/shared/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import { useDictation } from "@/hooks/use-dictation";
import { useSpeech } from "@/hooks/use-speech";

/** Seconds as m:ss, or plain seconds under a minute. */
function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

export default function InterviewSessionPage() {
  const router = useRouter();
  const params = useParams<{ sessionId: string }>();
  const sessionId = params.sessionId;

  const [session, setSession] = useState<InterviewSessionDetail | null>(null);
  const [activeIndex, setActiveIndex] = useState(0);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isCompleting, setIsCompleting] = useState(false);
  const [isSkipping, setIsSkipping] = useState(false);
  const [isRegenerating, setIsRegenerating] = useState(false);
  // Sticky for the whole answer: a candidate who dictated and then fixed a
  // word by hand still produced speech, and the transcript reads like it.
  const [dictated, setDictated] = useState(false);

  // Appends rather than replaces, so speech extends a typed draft instead of
  // destroying it, and a dictated answer survives being edited by hand.
  const dictation = useDictation(
    useCallback((text: string) => {
      if (!text) return;
      setDictated(true);
      setDraft((previous) => (previous ? `${previous} ${text}` : text));
    }, []),
  );

  // Local rendering only -- nothing leaves the browser, unlike dictation.
  const speech = useSpeech();
  // True while replacing an answer that already exists, which switches the
  // submit from POST to PUT.
  const [isEditing, setIsEditing] = useState(false);
  const [isAbandoning, setIsAbandoning] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);
  // Two-step confirmation: deleting takes the transcript and report with it,
  // and there is no undo.
  const [confirmDelete, setConfirmDelete] = useState(false);
  // Wall-clock start of the current question, used to report how long the
  // answer took. Held in a ref so the ticking display cannot restart it.
  const questionStartedAt = useRef<number>(Date.now());
  const [elapsed, setElapsed] = useState(0);

  const load = useCallback(async () => {
    const data = await api.get<InterviewSessionDetail>(`/interviews/${sessionId}`);
    setSession(data);
    // Jump to the first unanswered question.
    const firstUnanswered = data.questions.findIndex((q) => q.answer === null);
    setActiveIndex(firstUnanswered === -1 ? data.questions.length - 1 : firstUnanswered);
  }, [sessionId]);

  useEffect(() => {
    void load();
  }, [load]);

  const questions = session?.questions ?? [];
  const activeQuestion: Question | undefined = questions[activeIndex];

  // Moving to another question stops the voice. Without this it keeps reading a
  // question that is no longer on screen.
  const speechStop = speech.stop;
  useEffect(() => {
    speechStop();
  }, [activeQuestion?.id, speechStop]);
  const answeredCount = useMemo(
    () => questions.filter((q) => q.answer !== null).length,
    [questions]
  );
  const isCompleted = session?.status === "completed";
  const isAbandoned = session?.status === "abandoned";
  const isInProgress = session?.status === "in_progress";
  const isTiming = Boolean(
    isInProgress && activeQuestion && (!activeQuestion.answer || isEditing),
  );
  // Regenerating throws the question set away, so it is only offered while
  // there is nothing to lose.
  const canRegenerate = Boolean(
    isInProgress && questions.length > 0 && answeredCount === 0,
  );

  // Restart the clock whenever a different question becomes active.
  useEffect(() => {
    questionStartedAt.current = Date.now();
    setElapsed(0);
  }, [activeQuestion?.id]);

  // Tick only while an unanswered question is on screen.
  useEffect(() => {
    if (!isTiming) return;
    const id = setInterval(
      () => setElapsed(Math.floor((Date.now() - questionStartedAt.current) / 1000)),
      1000,
    );
    return () => clearInterval(id);
  }, [isTiming, activeQuestion?.id]);

  async function handleSkip() {
    if (!activeQuestion) return;
    setError(null);
    setIsSkipping(true);
    try {
      await api.post(
        `/interviews/${sessionId}/questions/${activeQuestion.id}/skip`,
      );
      await load();
      handleNext();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Unable to skip this question.");
    } finally {
      setIsSkipping(false);
    }
  }

  /** Reopen an answered question so it can be replaced. */
  function handleStartEdit() {
    if (!activeQuestion?.answer) return;
    setDraft(activeQuestion.answer.content);
    setDictated(false);
    setIsEditing(true);
    setError(null);
    // The clock restarts: the reported duration should describe the new attempt.
    questionStartedAt.current = Date.now();
    setElapsed(0);
  }

  async function handleRegenerate() {
    setError(null);
    setIsRegenerating(true);
    try {
      await api.post(`/interviews/${sessionId}/regenerate-questions`);
      setDraft("");
      setActiveIndex(0);
      await load();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Unable to regenerate the questions.",
      );
    } finally {
      setIsRegenerating(false);
    }
  }

  async function handleSubmitAnswer() {
    if (!activeQuestion || !draft.trim()) return;
    setError(null);
    setIsSubmitting(true);
    try {
      const body = {
        question_id: activeQuestion.id,
        content: draft.trim(),
        duration_seconds: Math.max(
          0,
          Math.round((Date.now() - questionStartedAt.current) / 1000),
        ),
        transcript_source: dictated ? "spoken" : "typed",
      };
      // PUT replaces an existing answer (and its follow-up); POST creates one.
      const answer = isEditing
        ? await api.put<Answer>(`/interviews/${sessionId}/answers`, body)
        : await api.post<Answer>(`/interviews/${sessionId}/answers`, body);
      // Reflect locally, then reload to pick up any AI follow-up question.
      setDraft("");
      setDictated(false);
      setIsEditing(false);
      setSession((prev) =>
        prev
          ? {
              ...prev,
              questions: prev.questions.map((q) =>
                q.id === activeQuestion.id ? { ...q, answer } : q
              ),
            }
          : prev
      );
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Unable to submit your answer.");
    } finally {
      setIsSubmitting(false);
    }
  }

  function handleNext() {
    setDraft("");
    setError(null);
    setIsEditing(false);
    setActiveIndex((i) => Math.min(i + 1, questions.length - 1));
  }

  async function handleComplete() {
    setError(null);
    setIsCompleting(true);
    try {
      await api.post(`/interviews/${sessionId}/complete`);
      router.push(`/interviews/${sessionId}/report`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Unable to end the interview.");
      setIsCompleting(false);
    }
  }

  async function handleAbandon() {
    setError(null);
    setIsAbandoning(true);
    try {
      await api.post(`/interviews/${sessionId}/abandon`);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Unable to abandon the interview.");
    } finally {
      setIsAbandoning(false);
    }
  }

  async function handleDelete() {
    setError(null);
    setIsDeleting(true);
    try {
      await api.delete(`/interviews/${sessionId}`);
      router.push("/interviews");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Unable to delete the interview.");
      setIsDeleting(false);
    }
  }

  if (!session) {
    return <p className="text-sm text-muted-foreground">Loading session…</p>;
  }

  return (
    <>
      <PageHeader
        title={session.title}
        description={session.target_role ?? "General interview"}
        actions={
          <Badge variant={isCompleted ? "success" : "secondary"}>
            {session.status.replace("_", " ")}
          </Badge>
        }
      />

      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm text-muted-foreground">
          {answeredCount} of {questions.length} questions answered
        </p>
        {canRegenerate && (
          <Button
            variant="outline"
            size="sm"
            onClick={handleRegenerate}
            disabled={isRegenerating}
            title="Replace these questions with a new set"
          >
            <Wand2 className="h-4 w-4" aria-hidden />
            {isRegenerating ? "Regenerating…" : "Regenerate questions"}
          </Button>
        )}
      </div>

      {isCompleted ? (
        <Card>
          <CardContent className="flex flex-col items-center gap-4 py-12 text-center">
            <CheckCircle2 className="h-10 w-10 text-primary" aria-hidden />
            <div>
              <p className="font-medium">This interview is complete.</p>
              <p className="text-sm text-muted-foreground">
                Scoring runs in the background — open the report to watch it land.
              </p>
            </div>
            <Link href={`/interviews/${sessionId}/report`}>
              <Button>View report</Button>
            </Link>
          </CardContent>
        </Card>
      ) : isInProgress && activeQuestion ? (
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between">
              <CardTitle className="text-base">
                Question {activeIndex + 1} of {questions.length}
              </CardTitle>
              <div className="flex items-center gap-2">
                {isTiming && (
                  <span
                    className="flex items-center gap-1 text-xs tabular-nums text-muted-foreground"
                    aria-label={`Time on this question: ${formatDuration(elapsed)}`}
                  >
                    <Timer className="h-3.5 w-3.5" aria-hidden />
                    {formatDuration(elapsed)}
                  </span>
                )}
                {activeQuestion.skipped && !activeQuestion.answer && (
                  <Badge variant="secondary">skipped</Badge>
                )}
                <Badge variant="outline">{activeQuestion.question_type.replace("_", " ")}</Badge>
              </div>
            </div>
            <CardDescription className="pt-2 text-base text-foreground">
              {activeQuestion.content}
            </CardDescription>
            {speech.supported && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="mt-2 h-auto px-2 py-1 text-xs"
                onClick={() =>
                  speech.speaking ? speech.stop() : speech.speak(activeQuestion.content)
                }
                aria-label={
                  speech.speaking ? "Stop reading the question" : "Read the question aloud"
                }
              >
                {speech.speaking ? "Stop reading" : "Read aloud"}
              </Button>
            )}
          </CardHeader>
          <CardContent className="space-y-4">
            {activeQuestion.answer && !isEditing ? (
              <div className="rounded-md border bg-secondary/50 p-3">
                <div className="flex items-center justify-between gap-2">
                  <p className="text-xs font-medium text-muted-foreground">Your answer</p>
                  {activeQuestion.answer.duration_seconds !== null && (
                    <span className="flex items-center gap-1 text-xs tabular-nums text-muted-foreground">
                      <Timer className="h-3.5 w-3.5" aria-hidden />
                      answered in {formatDuration(activeQuestion.answer.duration_seconds)}
                    </span>
                  )}
                </div>
                <p className="mt-1 text-sm">{activeQuestion.answer.content}</p>
                {activeQuestion.answer.transcript_source === "spoken" && (
                  <p className="mt-1 text-xs text-muted-foreground">Dictated</p>
                )}
              </div>
            ) : (
              <div className="space-y-2">
                <Textarea
                  value={
                    dictation.interim ? `${draft} ${dictation.interim}`.trim() : draft
                  }
                  onChange={(e) => setDraft(e.target.value)}
                  placeholder="Type your answer here…"
                  aria-label="Your answer"
                />

                {dictation.supported && (
                  <div className="flex flex-wrap items-center gap-2">
                    <Button
                      type="button"
                      variant={dictation.listening ? "destructive" : "outline"}
                      size="sm"
                      onClick={dictation.listening ? dictation.stop : dictation.start}
                      aria-pressed={dictation.listening}
                    >
                      {dictation.listening ? "Stop dictating" : "Dictate answer"}
                    </Button>

                    {dictation.listening ? (
                      <span
                        role="status"
                        className="text-xs text-muted-foreground"
                      >
                        Listening — your speech is sent to your browser&apos;s speech
                        service for transcription. No recording is kept.
                      </span>
                    ) : (
                      <span className="text-xs text-muted-foreground">
                        Speaking sends audio to your browser&apos;s speech service.
                      </span>
                    )}
                  </div>
                )}

                {dictation.error && (
                  <p role="alert" className="text-xs text-destructive">
                    {dictation.error}
                  </p>
                )}
              </div>
            )}

            {error && (
              <p role="alert" className="text-sm text-destructive">
                {error}
              </p>
            )}

            <div className="flex flex-wrap items-center gap-2">
              {(!activeQuestion.answer || isEditing) && (
                <Button onClick={handleSubmitAnswer} disabled={isSubmitting || !draft.trim()}>
                  {isSubmitting
                    ? "Saving…"
                    : isEditing
                      ? "Save new answer"
                      : "Submit answer"}
                </Button>
              )}
              {isEditing && (
                <Button
                  variant="outline"
                  onClick={() => {
                    setIsEditing(false);
                    setDraft("");
                  }}
                  disabled={isSubmitting}
                >
                  Cancel
                </Button>
              )}
              {activeQuestion.answer && !isEditing && (
                <Button variant="outline" onClick={handleStartEdit}>
                  <Pencil className="h-4 w-4" aria-hidden />
                  Change answer
                </Button>
              )}
              {!activeQuestion.answer && !isEditing && (
                <Button variant="outline" onClick={handleSkip} disabled={isSkipping}>
                  <SkipForward className="h-4 w-4" aria-hidden />
                  {isSkipping ? "Skipping…" : activeQuestion.skipped ? "Skipped" : "Skip"}
                </Button>
              )}
              {activeIndex < questions.length - 1 && (
                <Button variant="outline" onClick={handleNext}>
                  Next question
                  <ChevronRight className="h-4 w-4" aria-hidden />
                </Button>
              )}
              <Button
                variant="destructive"
                className="ml-auto"
                onClick={handleComplete}
                disabled={isCompleting}
              >
                {isCompleting ? "Ending…" : "End interview"}
              </Button>
            </div>
          </CardContent>
        </Card>
      ) : isInProgress ? (
        <p className="text-sm text-muted-foreground">No questions were generated.</p>
      ) : null}

      {isAbandoned && (
        <Card>
          <CardContent className="py-6 text-center">
            <p className="text-sm text-muted-foreground">
              This interview was abandoned. The transcript is kept for reference, but no
              evaluation report was generated.
            </p>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Manage this session</CardTitle>
          <CardDescription>
            Abandoning stops the interview but keeps the transcript. Deleting removes it
            permanently, along with any report.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap items-center gap-2">
          {isInProgress && (
            <Button variant="outline" onClick={handleAbandon} disabled={isAbandoning}>
              {isAbandoning ? "Abandoning…" : "Abandon interview"}
            </Button>
          )}

          {confirmDelete ? (
            <>
              <Button variant="destructive" onClick={handleDelete} disabled={isDeleting}>
                {isDeleting ? "Deleting…" : "Yes, delete permanently"}
              </Button>
              <Button
                variant="outline"
                onClick={() => setConfirmDelete(false)}
                disabled={isDeleting}
              >
                Cancel
              </Button>
            </>
          ) : (
            <Button variant="outline" onClick={() => setConfirmDelete(true)}>
              <Trash2 className="h-4 w-4" aria-hidden />
              Delete session
            </Button>
          )}
        </CardContent>
      </Card>
    </>
  );
}
