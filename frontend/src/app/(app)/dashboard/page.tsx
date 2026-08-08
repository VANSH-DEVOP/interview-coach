"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { BarChart3, FileText, MessageSquare, TrendingUp } from "lucide-react";

import { api } from "@/lib/api-client";
import type {
  EvaluationReport,
  InterviewSession,
  Page,
  ProgressSummary,
  Resume,
} from "@/types";
import { PageHeader } from "@/components/shared/page-header";
import { ScoreTrend } from "@/components/shared/score-trend";
import { StatCard } from "@/components/shared/stat-card";
import { Badge } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";

interface DashboardData {
  interviews: Page<InterviewSession>;
  resumes: Page<Resume>;
  reports: Page<EvaluationReport>;
  progress: ProgressSummary;
}

/** "+1.4" / "-0.7" / "0.0" — sign is the point, so always show it. */
function formatDelta(value: number): string {
  return `${value > 0 ? "+" : ""}${value.toFixed(1)}`;
}

export default function DashboardPage() {
  const [data, setData] = useState<DashboardData | null>(null);

  useEffect(() => {
    void Promise.all([
      api.get<Page<InterviewSession>>("/interviews?page=1&size=5"),
      api.get<Page<Resume>>("/resumes?page=1&size=1"),
      api.get<Page<EvaluationReport>>("/reports?page=1&size=1"),
      api.get<ProgressSummary>("/reports/progress"),
    ]).then(([interviews, resumes, reports, progress]) =>
      setData({ interviews, resumes, reports, progress }),
    );
  }, []);

  const progress = data?.progress;

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

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          title="Interview sessions"
          value={data?.interviews.total ?? "—"}
          icon={MessageSquare}
        />
        <StatCard title="Resumes uploaded" value={data?.resumes.total ?? "—"} icon={FileText} />
        <StatCard title="Evaluation reports" value={data?.reports.total ?? "—"} icon={BarChart3} />
        <StatCard
          title="Average score"
          value={progress?.average_score != null ? progress.average_score.toFixed(1) : "—"}
          icon={TrendingUp}
        />
      </div>

      <Card>
        <CardHeader className="flex flex-row items-start justify-between space-y-0">
          <div>
            <CardTitle className="text-base">Progress over time</CardTitle>
            {progress && progress.total_scored > 0 && (
              <p className="pt-1 text-xs text-muted-foreground">
                {progress.total_scored} scored{" "}
                {progress.total_scored === 1 ? "interview" : "interviews"} · best{" "}
                {progress.best_score?.toFixed(1)} · latest {progress.latest_score?.toFixed(1)}
              </p>
            )}
          </div>
          {progress?.improvement != null && (
            <Badge variant={progress.improvement >= 0 ? "success" : "destructive"}>
              {formatDelta(progress.improvement)} recent
            </Badge>
          )}
        </CardHeader>
        <CardContent className="space-y-4">
          {progress && progress.total_scored === 0 ? (
            <p className="text-sm text-muted-foreground">
              Complete an interview and your score trend will appear here.
            </p>
          ) : (
            <>
              {progress && <ScoreTrend points={progress.points} />}
              {progress && progress.improvement === null && progress.total_scored > 0 && (
                <p className="text-xs text-muted-foreground">
                  Complete a few more interviews for a meaningful trend.
                </p>
              )}
              {progress && Object.keys(progress.average_by_type).length > 1 && (
                <div className="flex flex-wrap gap-2 border-t pt-3">
                  {Object.entries(progress.average_by_type).map(([type, avg]) => (
                    <span
                      key={type}
                      className="text-xs text-muted-foreground"
                      title={`Average score for ${type.replace("_", " ")} interviews`}
                    >
                      {type.replace("_", " ")}:{" "}
                      <span className="font-medium text-foreground tabular-nums">
                        {avg.toFixed(1)}
                      </span>
                    </span>
                  ))}
                </div>
              )}
            </>
          )}
        </CardContent>
      </Card>

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
