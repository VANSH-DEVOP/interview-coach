"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { MessageSquare } from "lucide-react";

import { api, ApiError } from "@/lib/api-client";
import type { InterviewSession, Page } from "@/types";
import { PageHeader } from "@/components/shared/page-header";
import { EmptyState } from "@/components/shared/empty-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export default function InterviewsPage() {
  const [sessions, setSessions] = useState<Page<InterviewSession> | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  const load = useCallback(async () => {
    setSessions(await api.get<Page<InterviewSession>>("/interviews?page=1&size=20"));
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function handleCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setIsSubmitting(true);

    const form = new FormData(event.currentTarget);
    try {
      await api.post<InterviewSession>("/interviews", {
        title: String(form.get("title")),
        target_role: String(form.get("target_role")) || null,
      });
      setShowForm(false);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Unable to create the session.");
    } finally {
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
              Adaptive AI questioning will use this context once enabled.
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
            <Card key={session.id}>
              <CardHeader className="flex flex-row items-start justify-between space-y-0">
                <div>
                  <CardTitle className="text-base">{session.title}</CardTitle>
                  <CardDescription>{session.target_role ?? "General interview"}</CardDescription>
                </div>
                <Badge variant={session.status === "completed" ? "success" : "secondary"}>
                  {session.status.replace("_", " ")}
                </Badge>
              </CardHeader>
              <CardContent>
                <p className="text-xs text-muted-foreground">
                  Created {new Date(session.created_at).toLocaleString()}
                </p>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </>
  );
}
