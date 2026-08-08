"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";

import { Button } from "@/components/ui/button";

interface PaginationProps {
  page: number;
  size: number;
  total: number;
  onPageChange: (page: number) => void;
  /** Plural noun for the counter, e.g. "interviews". */
  label?: string;
}

/**
 * Prev/next controls for the API's `Page<T>` envelope.
 *
 * Renders nothing when everything fits on one page, so callers can drop it in
 * unconditionally.
 */
export function Pagination({ page, size, total, onPageChange, label = "items" }: PaginationProps) {
  const totalPages = Math.max(1, Math.ceil(total / size));
  if (total === 0 || totalPages === 1) return null;

  const first = (page - 1) * size + 1;
  const last = Math.min(page * size, total);

  return (
    <nav
      className="flex items-center justify-between gap-4 border-t pt-4"
      aria-label={`${label} pagination`}
    >
      <p className="text-xs text-muted-foreground tabular-nums">
        {first}–{last} of {total} {label}
      </p>
      <div className="flex items-center gap-2">
        <Button
          variant="outline"
          size="sm"
          onClick={() => onPageChange(page - 1)}
          disabled={page <= 1}
          aria-label="Previous page"
        >
          <ChevronLeft className="h-4 w-4" aria-hidden />
          Previous
        </Button>
        <span className="text-xs text-muted-foreground tabular-nums" aria-current="page">
          Page {page} of {totalPages}
        </span>
        <Button
          variant="outline"
          size="sm"
          onClick={() => onPageChange(page + 1)}
          disabled={page >= totalPages}
          aria-label="Next page"
        >
          Next
          <ChevronRight className="h-4 w-4" aria-hidden />
        </Button>
      </div>
    </nav>
  );
}
