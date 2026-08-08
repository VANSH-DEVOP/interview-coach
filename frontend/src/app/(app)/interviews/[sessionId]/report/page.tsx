"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { RefreshCw } from "lucide-react";

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
  const [isReevaluating, setIsReevaluating] = useState(false);

  useEffect(() => {
    api
      .get<EvaluationReport>(`/reports/by-session/${sessionId}`)
      .then(setReport)
      .catch((err) =>
        setError(err instanceof ApiError ? err.message : "Unable to load the report.")
      );
  }, [sessionId]);

  /** Re-run the evaluator over the same transcript, replacing this report. */
  async function handleReevaluate() {
    setError(null);
    setIsReevaluating(true);
    try {
      setReport(await api.post<EvaluationReport>(`/interviews/${sessionId}/reevaluate`));
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Unable to regenerate the report."
      );
    } finally {
      setIsReevaluating(false);
    }
  }

  return (
    <>
      <PageHeader
        title="Interview report"
        description="AI-generated evaluation of your session."
        actions={
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              onClick={handleReevaluate}
              disabled={isReevaluating || report === null}
              title="Score this transcript again"
            >
              <RefreshCw
                className={`h-4 w-4 ${isReevaluating ? "animate-spin" : ""}`}
                aria-hidden
              />
              {isReevaluating ? "Re-evaluating…" : "Re-evaluate"}
            </Button>
            <Link href="/interviews">
              <Button variant="outline">Back to interviews</Button>
            </Link>
          </div>
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
