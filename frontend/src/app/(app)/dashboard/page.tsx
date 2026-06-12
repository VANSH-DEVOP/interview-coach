"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { BarChart3, FileText, MessageSquare } from "lucide-react";

import { api } from "@/lib/api-client";
import type { EvaluationReport, InterviewSession, Page, Resume } from "@/types";
import { PageHeader } from "@/components/shared/page-header";
import { StatCard } from "@/components/shared/stat-card";
import { Badge } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";

interface DashboardData {
  interviews: Page<InterviewSession>;
  resumes: Page<Resume>;
  reports: Page<EvaluationReport>;
}

export default function DashboardPage() {
  const [data, setData] = useState<DashboardData | null>(null);

  useEffect(() => {
    void Promise.all([
      api.get<Page<InterviewSession>>("/interviews?page=1&size=5"),
      api.get<Page<Resume>>("/resumes?page=1&size=1"),
      api.get<Page<EvaluationReport>>("/reports?page=1&size=1"),
    ]).then(([interviews, resumes, reports]) => setData({ interviews, resumes, reports }));
  }, []);

  return (
    <>
      <PageHeader
        title="Dashboard"
        description="Your interview preparation at a glance."
        actions={
          <Link href="/interviews" className={cn(buttonVariants())}>
            Start an interview
          </Link>
        }
      />

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <StatCard
          title="Interview sessions"
          value={data?.interviews.total ?? "—"}
          icon={MessageSquare}
        />
        <StatCard title="Resumes uploaded" value={data?.resumes.total ?? "—"} icon={FileText} />
        <StatCard title="Evaluation reports" value={data?.reports.total ?? "—"} icon={BarChart3} />
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Recent sessions</CardTitle>
        </CardHeader>
        <CardContent>
          {data && data.interviews.items.length === 0 && (
            <p className="text-sm text-muted-foreground">
              No interview sessions yet. Start your first one from the Interviews page.
            </p>
          )}
          <ul className="divide-y">
            {data?.interviews.items.map((session) => (
              <li key={session.id} className="flex items-center justify-between py-3">
                <div>
                  <p className="text-sm font-medium">{session.title}</p>
                  <p className="text-xs text-muted-foreground">
                    {session.target_role ?? "General"} ·{" "}
                    {new Date(session.created_at).toLocaleDateString()}
                  </p>
                </div>
                <Badge variant={session.status === "completed" ? "success" : "secondary"}>
                  {session.status.replace("_", " ")}
                </Badge>
              </li>
            ))}
          </ul>
        </CardContent>
      </Card>
    </>
  );
}
