"use client";

import { useCallback, useEffect, useRef, useState, type ChangeEvent } from "react";
import { Download, Eye, FileText, RefreshCw, Trash2, Upload } from "lucide-react";

import { api, ApiError, getAccessToken } from "@/lib/api-client";
import type { Page, Resume, ResumePreview } from "@/types";
import { PageHeader } from "@/components/shared/page-header";
import { EmptyState } from "@/components/shared/empty-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";

const ACCEPTED =
  ".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api/v1";

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export default function ResumesPage() {
  const [resumes, setResumes] = useState<Page<Resume> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const [reprocessingId, setReprocessingId] = useState<string | null>(null);
  const [previewId, setPreviewId] = useState<string | null>(null);
  const [preview, setPreview] = useState<ResumePreview | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    setResumes(await api.get<Page<Resume>>("/resumes?page=1&size=50"));
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function handleFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    setError(null);
    setIsUploading(true);
    try {
      const form = new FormData();
      form.append("file", file);
      await api.postForm<Resume>("/resumes", form);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Upload failed.");
    } finally {
      setIsUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }

  async function handlePreview(id: string) {
    // Toggle closed if this one is already open.
    if (previewId === id) {
      setPreviewId(null);
      return;
    }
    setError(null);
    setPreviewId(id);
    setPreview(null);
    try {
      setPreview(await api.get<ResumePreview>(`/resumes/${id}/preview`));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Unable to load the preview.");
      setPreviewId(null);
    }
  }

  async function handleReprocess(id: string) {
    setError(null);
    setReprocessingId(id);
    try {
      await api.post<Resume>(`/resumes/${id}/reprocess`);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Reprocessing failed.");
    } finally {
      setReprocessingId(null);
    }
  }

  async function handleDelete(id: string) {
    setError(null);
    try {
      await api.delete<void>(`/resumes/${id}`);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Delete failed.");
    }
  }

  async function handleDownload(resume: Resume) {
    // Authenticated binary download: fetch with the bearer token, then save.
    setError(null);
    try {
      const response = await fetch(`${API_URL}/resumes/${resume.id}/download`, {
        headers: { Authorization: `Bearer ${getAccessToken() ?? ""}` },
      });
      if (!response.ok) throw new Error("Download failed.");
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = resume.file_name;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch {
      setError("Unable to download this resume.");
    }
  }

  return (
    <>
      <PageHeader
        title="Resumes"
        description="Upload a resume to personalize your mock interviews."
        actions={
          <>
            <input
              ref={fileInputRef}
              type="file"
              accept={ACCEPTED}
              className="hidden"
              onChange={handleFile}
            />
            <Button onClick={() => fileInputRef.current?.click()} disabled={isUploading}>
              <Upload className="h-4 w-4" aria-hidden />
              {isUploading ? "Uploading..." : "Upload resume"}
            </Button>
          </>
        }
      />

      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}

      {resumes && resumes.items.length === 0 ? (
        <EmptyState
          icon={FileText}
          title="No resumes uploaded"
          description="Upload a PDF or DOCX resume (max 5 MiB) to get tailored interview questions."
          action={
            <Button onClick={() => fileInputRef.current?.click()} disabled={isUploading}>
              <Upload className="h-4 w-4" aria-hidden />
              Upload resume
            </Button>
          }
        />
      ) : (
        <div className="space-y-3">
          {resumes?.items.map((resume) => (
            <Card key={resume.id}>
              <CardContent className="flex items-center justify-between gap-4 p-4">
                <div className="flex items-center gap-3 min-w-0">
                  <FileText className="h-5 w-5 shrink-0 text-primary" aria-hidden />
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium">{resume.file_name}</p>
                    <p className="text-xs text-muted-foreground">
                      {formatSize(resume.size_bytes)} ·{" "}
                      {new Date(resume.created_at).toLocaleDateString()}
                    </p>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <Badge variant={resume.status === "failed" ? "destructive" : "secondary"}>
                    {resume.status}
                  </Badge>
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label="Preview extracted text"
                    title="See the text the AI reads from this resume"
                    onClick={() => handlePreview(resume.id)}
                  >
                    <Eye className="h-4 w-4" aria-hidden />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label="Reprocess resume"
                    title="Re-parse this resume and rebuild its search index"
                    disabled={reprocessingId === resume.id}
                    onClick={() => handleReprocess(resume.id)}
                  >
                    <RefreshCw
                      className={`h-4 w-4 ${reprocessingId === resume.id ? "animate-spin" : ""}`}
                      aria-hidden
                    />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label="Download resume"
                    onClick={() => handleDownload(resume)}
                  >
                    <Download className="h-4 w-4" aria-hidden />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label="Delete resume"
                    onClick={() => handleDelete(resume.id)}
                  >
                    <Trash2 className="h-4 w-4" aria-hidden />
                  </Button>
                </div>
              </CardContent>

              {previewId === resume.id && (
                <CardContent className="border-t pt-4">
                  {preview === null ? (
                    <p className="text-sm text-muted-foreground">Loading preview…</p>
                  ) : preview.parsed_text ? (
                    <>
                      <p className="mb-2 text-xs text-muted-foreground">
                        {preview.word_count.toLocaleString()} words ·{" "}
                        {preview.character_count.toLocaleString()} characters — this is
                        what question generation reads.
                      </p>
                      <pre className="max-h-72 overflow-auto whitespace-pre-wrap rounded-md bg-secondary/50 p-3 text-xs leading-relaxed">
                        {preview.parsed_text}
                      </pre>
                    </>
                  ) : (
                    <p className="text-sm text-muted-foreground">
                      No text could be extracted from this file, so interviews using it
                      will not be personalised. Try reprocessing it, or upload a
                      text-based PDF rather than a scan.
                    </p>
                  )}
                </CardContent>
              )}
            </Card>
          ))}
        </div>
      )}
    </>
  );
}
