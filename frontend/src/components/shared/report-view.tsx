import { CheckCircle2, Lightbulb, XCircle } from "lucide-react";

import type { EvaluationReport } from "@/types";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

const STATUS_VARIANT: Record<EvaluationReport["status"], "success" | "secondary" | "destructive"> = {
  completed: "success",
  pending: "secondary",
  generating: "secondary",
  failed: "destructive",
};

function ScoreRing({ score }: { score: number }) {
  const pct = Math.max(0, Math.min(score / 10, 1)) * 100;
  return (
    <div className="flex items-center gap-4">
      <div
        className="flex h-20 w-20 items-center justify-center rounded-full text-xl font-semibold"
        style={{
          background: `conic-gradient(hsl(var(--primary)) ${pct}%, hsl(var(--secondary)) ${pct}%)`,
        }}
      >
        <span className="flex h-16 w-16 items-center justify-center rounded-full bg-card">
          {score.toFixed(1)}
        </span>
      </div>
      <div>
        <p className="text-sm text-muted-foreground">Overall score</p>
        <p className="text-2xl font-semibold">{score.toFixed(1)} / 10</p>
      </div>
    </div>
  );
}

export function ReportView({ report }: { report: EvaluationReport }) {
  const score = report.overall_score !== null ? Number(report.overall_score) : null;
  const feedback = report.detailed_feedback;
  const recommendations = feedback?.recommendations ?? [];
  const perQuestion = feedback?.per_question ?? [];

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader className="flex flex-row items-center justify-between space-y-0">
          <CardTitle className="text-base">Evaluation summary</CardTitle>
          <Badge variant={STATUS_VARIANT[report.status]}>{report.status}</Badge>
        </CardHeader>
        <CardContent className="space-y-4">
          {score !== null ? <ScoreRing score={score} /> : (
            <p className="text-sm text-muted-foreground">Awaiting evaluation.</p>
          )}
          {feedback?.summary && <p className="text-sm">{feedback.summary}</p>}
        </CardContent>
      </Card>

      <div className="grid gap-4 sm:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <CheckCircle2 className="h-4 w-4 text-primary" aria-hidden />
              Strengths
            </CardTitle>
          </CardHeader>
          <CardContent>
            {report.strengths && report.strengths.length > 0 ? (
              <ul className="list-disc space-y-1 pl-5 text-sm">
                {report.strengths.map((s, i) => (
                  <li key={i}>{s}</li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-muted-foreground">None identified.</p>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <XCircle className="h-4 w-4 text-destructive" aria-hidden />
              Areas to improve
            </CardTitle>
          </CardHeader>
          <CardContent>
            {report.weaknesses && report.weaknesses.length > 0 ? (
              <ul className="list-disc space-y-1 pl-5 text-sm">
                {report.weaknesses.map((w, i) => (
                  <li key={i}>{w}</li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-muted-foreground">None identified.</p>
            )}
          </CardContent>
        </Card>
      </div>

      {recommendations.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Lightbulb className="h-4 w-4 text-primary" aria-hidden />
              Recommendations
            </CardTitle>
          </CardHeader>
          <CardContent>
            <ul className="list-disc space-y-1 pl-5 text-sm">
              {recommendations.map((r, i) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}

      {perQuestion.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Question-by-question feedback</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            {perQuestion.map((item, i) => (
              <div key={i} className="border-b pb-3 last:border-0 last:pb-0">
                <p className="text-sm font-medium">{item.question}</p>
                <p className="mt-1 text-sm text-muted-foreground">{item.feedback}</p>
              </div>
            ))}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
