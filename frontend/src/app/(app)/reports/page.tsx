"use client";

import { useEffect, useState } from "react";
import { BarChart3 } from "lucide-react";

import { api } from "@/lib/api-client";
import type { EvaluationReport, Page } from "@/types";
import { PageHeader } from "@/components/shared/page-header";
import { EmptyState } from "@/components/shared/empty-state";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

const STATUS_VARIANT: Record<EvaluationReport["status"], "success" | "secondary" | "destructive"> =
  {
    completed: "success",
    pending: "secondary",
    generating: "secondary",
    failed: "destructive",
  };

export default function ReportsPage() {
  const [reports, setReports] = useState<Page<EvaluationReport> | null>(null);

  useEffect(() => {
    void api.get<Page<EvaluationReport>>("/reports?page=1&size=20").then(setReports);
  }, []);

  return (
    <>
      <PageHeader
        title="Reports"
        description="Evaluation reports from your completed interview sessions."
      />

      {reports && reports.items.length === 0 ? (
        <EmptyState
          icon={BarChart3}
          title="No reports yet"
          description="Complete an interview session and your evaluation report will appear here."
        />
      ) : (
        <div className="grid gap-4 sm:grid-cols-2">
          {reports?.items.map((report) => (
            <Card key={report.id}>
              <CardHeader className="flex flex-row items-start justify-between space-y-0">
                <div>
                  <CardTitle className="text-base">
                    {report.overall_score !== null
                      ? `Score: ${report.overall_score}`
                      : "Awaiting evaluation"}
                  </CardTitle>
                  <CardDescription>
                    {new Date(report.created_at).toLocaleString()}
                  </CardDescription>
                </div>
                <Badge variant={STATUS_VARIANT[report.status]}>{report.status}</Badge>
              </CardHeader>
              <CardContent>
                <p className="text-xs text-muted-foreground">
                  Detailed AI feedback becomes available once evaluation completes.
                </p>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </>
  );
}
