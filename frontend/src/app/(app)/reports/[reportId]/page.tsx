"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";

import { api, ApiError } from "@/lib/api-client";
import type { EvaluationReport } from "@/types";
import { PageHeader } from "@/components/shared/page-header";
import { Button } from "@/components/ui/button";
import { ReportView } from "@/components/shared/report-view";

export default function ReportDetailPage() {
  const params = useParams<{ reportId: string }>();
  const reportId = params.reportId;
  const [report, setReport] = useState<EvaluationReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .get<EvaluationReport>(`/reports/${reportId}`)
      .then(setReport)
      .catch((err) =>
        setError(err instanceof ApiError ? err.message : "Unable to load the report.")
      );
  }, [reportId]);

  return (
    <>
      <PageHeader
        title="Evaluation report"
        description="Detailed AI feedback for your interview."
        actions={
          <Link href="/reports">
            <Button variant="outline">Back to reports</Button>
          </Link>
        }
      />
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      {report ? (
        <ReportView report={report} />
      ) : (
        !error && <p className="text-sm text-muted-foreground">Loading report…</p>
      )}
    </>
  );
}
