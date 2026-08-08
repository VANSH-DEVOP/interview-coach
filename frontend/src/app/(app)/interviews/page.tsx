"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { MessageSquare } from "lucide-react";

import { api, ApiError } from "@/lib/api-client";
import {
  DEFAULT_QUESTION_COUNT,
  MAX_QUESTION_COUNT,
  MIN_QUESTION_COUNT,
  type DifficultyLevel,
  type InterviewSession,
  type InterviewType,
  type Page,
  type Resume,
} from "@/types";
import { PageHeader } from "@/components/shared/page-header";
import { EmptyState } from "@/components/shared/empty-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";

const INTERVIEW_TYPES: { value: InterviewType; label: string }[] = [
  { value: "mixed", label: "Mixed (behavioral + technical)" },
  { value: "behavioral", label: "Behavioral" },
  { value: "technical", label: "Technical" },
  { value: "system_design", label: "System design" },
];

const DIFFICULTIES: { value: DifficultyLevel; label: string }[] = [
  { value: "junior", label: "Junior (0-2 years)" },
  { value: "mid", label: "Mid (2-5 years)" },
  { value: "senior", label: "Senior (5+ years)" },
];

const QUESTION_COUNTS = Array.from(
  { length: MAX_QUESTION_COUNT - MIN_QUESTION_COUNT + 1 },
  (_, i) => MIN_QUESTION_COUNT + i,
);

export default function InterviewsPage() {
  const router = useRouter();
  const [sessions, setSessions] = useState<Page<InterviewSession> | null>(null);
  const [resumes, setResumes] = useState<Resume[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  const load = useCallback(async () => {
    const [sessionsPage, resumesPage] = await Promise.all([
      api.get<Page<InterviewSession>>("/interviews?page=1&size=20"),
      api.get<Page<Resume>>("/resumes?page=1&size=50"),
    ]);
    setSessions(sessionsPage);
    setResumes(resumesPage.items);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function handleCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setIsSubmitting(true);

    const form = new FormData(event.currentTarget);
    const resumeId = String(form.get("resume_id"));
    try {
      const session = await api.post<InterviewSession>("/interviews", {
        title: String(form.get("title")),
        target_role: String(form.get("target_role")) || null,
        resume_id: resumeId || null,
        interview_type: String(form.get("interview_type")) as InterviewType,
        difficulty: String(form.get("difficulty")) as DifficultyLevel,
        question_count: Number(form.get("question_count")),
      });
      router.push(`/interviews/${session.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Unable to create the session.");
      setIsSubmitting(false);
    }
  }

  return (
    <>
      <PageHeader
        title="Interviews"
        description="Run AI-powered mock interview sessions."
        actions={
          <Button onClick={() => setShowForm((v) => !v)}>
            {showForm ? "Cancel" : "New interview"}
          </Button>
        }
      />

      {showForm && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">New interview session</CardTitle>
            <CardDescription>
              Questions are generated from the target role and selected resume.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <form onSubmit={handleCreate} className="max-w-md space-y-4">
              <div className="space-y-2">
                <Label htmlFor="title">Session title</Label>
                <Input id="title" name="title" placeholder="e.g. Backend Engineer practice" required />
              </div>
              <div className="space-y-2">
                <Label htmlFor="target_role">Target role (optional)</Label>
                <Input id="target_role" name="target_role" placeholder="e.g. Senior Software Engineer" />
              </div>
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-2">
                  <Label htmlFor="interview_type">Interview type</Label>
                  <Select id="interview_type" name="interview_type" defaultValue="mixed">
                    {INTERVIEW_TYPES.map((t) => (
                      <option key={t.value} value={t.value}>
                        {t.label}
                      </option>
                    ))}
                  </Select>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="difficulty">Difficulty</Label>
                  <Select id="difficulty" name="difficulty" defaultValue="mid">
                    {DIFFICULTIES.map((d) => (
                      <option key={d.value} value={d.value}>
                        {d.label}
                      </option>
                    ))}
                  </Select>
                </div>
              </div>

              <div className="space-y-2">
                <Label htmlFor="question_count">Number of questions</Label>
                <Select
                  id="question_count"
                  name="question_count"
                  defaultValue={String(DEFAULT_QUESTION_COUNT)}
                >
                  {QUESTION_COUNTS.map((n) => (
                    <option key={n} value={n}>
                      {n}
                    </option>
                  ))}
                </Select>
              </div>

              <div className="space-y-2">
                <Label htmlFor="resume_id">Resume (optional)</Label>
                <Select id="resume_id" name="resume_id" defaultValue="">
                  <option value="">No resume</option>
                  {resumes.map((resume) => (
                    <option key={resume.id} value={resume.id}>
                      {resume.file_name}
                    </option>
                  ))}
                </Select>
                {resumes.length === 0 && (
                  <p className="text-xs text-muted-foreground">
                    No resumes yet.{" "}
                    <Link href="/resumes" className="text-primary underline">
                      Upload one
                    </Link>{" "}
                    for tailored questions.
                  </p>
                )}
              </div>

              {error && (
                <p role="alert" className="text-sm text-destructive">
                  {error}
                </p>
              )}

              <Button type="submit" disabled={isSubmitting}>
                {isSubmitting ? "Creating..." : "Start session"}
              </Button>
            </form>
          </CardContent>
        </Card>
      )}

      {sessions && sessions.items.length === 0 && !showForm ? (
        <EmptyState
          icon={MessageSquare}
          title="No interviews yet"
          description="Create your first mock interview session to start practicing."
          action={<Button onClick={() => setShowForm(true)}>New interview</Button>}
        />
      ) : (
        <div className="grid gap-4 sm:grid-cols-2">
          {sessions?.items.map((session) => (
            <Link key={session.id} href={`/interviews/${session.id}`} className="block">
              <Card className="transition-colors hover:border-primary">
                <CardHeader className="flex flex-row items-start justify-between space-y-0">
                  <div>
                    <CardTitle className="text-base">{session.title}</CardTitle>
                    <CardDescription>{session.target_role ?? "General interview"}</CardDescription>
                  </div>
                  <Badge variant={session.status === "completed" ? "success" : "secondary"}>
                    {session.status.replace("_", " ")}
                  </Badge>
                </CardHeader>
                <CardContent className="space-y-1">
                  <p className="text-xs text-muted-foreground">
                    {session.interview_type.replace("_", " ")} · {session.difficulty} ·{" "}
                    {session.question_count} questions
                  </p>
                  <p className="text-xs text-muted-foreground">
                    Created {new Date(session.created_at).toLocaleString()}
                  </p>
                </CardContent>
              </Card>
            </Link>
          ))}
        </div>
      )}
    </>
  );
}
