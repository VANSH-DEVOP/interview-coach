"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";

import { api, ApiError } from "@/lib/api-client";
import type { EvaluationReport } from "@/types";
import { PageHeader } from "@/components/shared/page-header";
import { Button } from "@/components/ui/button";
import { ReportView } from "@/components/shared/report-view";

export default function SessionReportPage() {
  const params = useParams<{ sessionId: string }>();
  const sessionId = params.sessionId;
  const [report, setReport] = useState<EvaluationReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .get<EvaluationReport>(`/reports/by-session/${sessionId}`)
      .then(setReport)
      .catch((err) =>
        setError(err instanceof ApiError ? err.message : "Unable to load the report.")
      );
  }, [sessionId]);

  return (
    <>
      <PageHeader
        title="Interview report"
        description="AI-generated evaluation of your session."
        actions={
          <Link href="/interviews">
            <Button variant="outline">Back to interviews</Button>
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
