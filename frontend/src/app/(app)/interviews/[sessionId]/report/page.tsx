"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { AlertTriangle, Download, Loader2, Printer, RefreshCw } from "lucide-react";

import { api, ApiError } from "@/lib/api-client";
import type { EvaluationReport } from "@/types";
import { PageHeader } from "@/components/shared/page-header";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { ReportView } from "@/components/shared/report-view";

/** Evaluation typically takes a few seconds; this trades a little latency for
 *  far fewer requests than a tight loop. */
const POLL_INTERVAL_MS = 2000;


export default function SessionReportPage() {
  const params = useParams<{ sessionId: string }>();
  const sessionId = params.sessionId;
  const [report, setReport] = useState<EvaluationReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isReevaluating, setIsReevaluating] = useState(false);

  const isGenerating =
    report !== null && (report.status === "pending" || report.status === "generating");

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        const next = await api.get<EvaluationReport>(`/reports/by-session/${sessionId}`);
        if (!cancelled) setReport(next);
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Unable to load the report.");
        }
      }
    }

    void load();
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  // Evaluation runs in the background, so the report arrives PENDING and is
  // filled in later. Poll until it settles, then stop -- a completed or failed
  // report will not change on its own.
  useEffect(() => {
    if (!isGenerating) return;

    const timer = setInterval(async () => {
      try {
        setReport(await api.get<EvaluationReport>(`/reports/by-session/${sessionId}`));
      } catch {
        // A transient failure while polling is not worth surfacing; the next
        // tick retries, and a genuinely broken report ends up FAILED anyway.
      }
    }, POLL_INTERVAL_MS);

    return () => clearInterval(timer);
  }, [isGenerating, sessionId]);

  /** Re-run the evaluator over the same transcript, replacing this report. */
  async function handleReevaluate() {
    setError(null);
    setIsReevaluating(true);
    try {
      // Comes back PENDING; the polling effect above takes it from there.
      setReport(await api.post<EvaluationReport>(`/interviews/${sessionId}/reevaluate`));
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Unable to regenerate the report."
      );
    } finally {
      setIsReevaluating(false);
    }
  }

  /** Download the Markdown export. Needs a manual fetch to carry the bearer
   *  token and read the filename the server chose. */
  async function handleExport() {
    if (!report) return;
    setError(null);
    try {
      const response = await fetch(`/api/bff/reports/${report.id}/export`);
      if (!response.ok) throw new Error("Export failed.");

      const disposition = response.headers.get("content-disposition") ?? "";
      const match = disposition.match(/filename="([^"]+)"/);
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = match?.[1] ?? "interview-report.md";
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch {
      setError("Unable to export this report.");
    }
  }

  const canExport = report?.status === "completed";

  return (
    <>
      <PageHeader
        title="Interview report"
        description="AI-generated evaluation of your session."
        actions={
          <div className="flex items-center gap-2 print:hidden">
            <Button
              variant="outline"
              onClick={() => window.print()}
              disabled={!canExport}
              title="Print, or save as PDF from the print dialog"
            >
              <Printer className="h-4 w-4" aria-hidden />
              Print / PDF
            </Button>
            <Button
              variant="outline"
              onClick={handleExport}
              disabled={!canExport}
              title="Download as Markdown"
            >
              <Download className="h-4 w-4" aria-hidden />
              Export
            </Button>
            <Button
              variant="outline"
              onClick={handleReevaluate}
              // No point queueing another run while one is already in flight.
              disabled={isReevaluating || report === null || isGenerating}
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
      {isGenerating && (
        <Card>
          <CardContent className="flex flex-col items-center gap-3 py-12 text-center">
            <Loader2 className="h-8 w-8 animate-spin text-primary" aria-hidden />
            <div>
              <p className="font-medium">Scoring your interview…</p>
              <p className="text-sm text-muted-foreground">
                This usually takes a few seconds. The page updates on its own.
              </p>
            </div>
          </CardContent>
        </Card>
      )}

      {report?.status === "failed" && (
        <Card>
          <CardContent className="flex flex-col items-center gap-3 py-12 text-center">
            <AlertTriangle className="h-8 w-8 text-destructive" aria-hidden />
            <div>
              <p className="font-medium">The evaluation didn&apos;t finish.</p>
              <p className="text-sm text-muted-foreground">
                Your answers are safe — only the scoring failed. Try again.
              </p>
            </div>
            <Button onClick={handleReevaluate} disabled={isReevaluating}>
              {isReevaluating ? "Retrying…" : "Retry evaluation"}
            </Button>
          </CardContent>
        </Card>
      )}

      {report?.status === "completed" && <ReportView report={report} />}

      {!report && !error && (
        <p className="text-sm text-muted-foreground">Loading report…</p>
      )}
    </>
  );
}
