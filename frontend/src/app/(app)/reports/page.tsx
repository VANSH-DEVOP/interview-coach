"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { BarChart3 } from "lucide-react";

import { api } from "@/lib/api-client";
import type { EvaluationReport, Page } from "@/types";
import { PageHeader } from "@/components/shared/page-header";
import { EmptyState } from "@/components/shared/empty-state";
import { Pagination } from "@/components/shared/pagination";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

const STATUS_VARIANT: Record<EvaluationReport["status"], "success" | "secondary" | "destructive"> =
  {
    completed: "success",
    pending: "secondary",
    generating: "secondary",
    failed: "destructive",
  };

const PAGE_SIZE = 20;

export default function ReportsPage() {
  const [reports, setReports] = useState<Page<EvaluationReport> | null>(null);
  const [page, setPage] = useState(1);

  useEffect(() => {
    void api
      .get<Page<EvaluationReport>>(`/reports?page=${page}&size=${PAGE_SIZE}`)
      .then(setReports);
  }, [page]);

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
        <div className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2">
          {reports?.items.map((report) => (
            <Link key={report.id} href={`/reports/${report.id}`} className="block">
              <Card className="transition-colors hover:border-primary">
                <CardHeader className="flex flex-row items-start justify-between space-y-0">
                  <div>
                    <CardTitle className="text-base">
                      {report.overall_score !== null
                        ? `Score: ${report.overall_score} / 10`
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
                    View detailed strengths, weaknesses, and recommendations.
                  </p>
                </CardContent>
              </Card>
            </Link>
          ))}
        </div>
        {reports && (
          <Pagination
            page={reports.page}
            size={reports.size}
            total={reports.total}
            onPageChange={setPage}
            label="reports"
          />
        )}
        </div>
      )}
    </>
  );
}
