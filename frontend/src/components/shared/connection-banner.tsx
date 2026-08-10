"use client";

import { useState } from "react";
import { CloudOff } from "lucide-react";

import type { ApiError } from "@/lib/api-client";
import { Button } from "@/components/ui/button";

/**
 * Says the server is unreachable, rather than letting the app imply a logout.
 *
 * The alternative this replaces was silence: a failed `/users/me` emptied the
 * user and the shell rendered as though the session had ended, so the obvious
 * response was to sign in again -- against a backend that could not answer
 * that either.
 *
 * Deliberately not a modal or a redirect. The page's own data may have loaded
 * before the outage, and half a dashboard with an honest banner over it beats
 * an empty screen.
 */
export function ConnectionBanner({
  error,
  onRetry,
}: {
  error: ApiError | null;
  onRetry: () => void | Promise<void>;
}) {
  const [retrying, setRetrying] = useState(false);

  if (!error) return null;

  async function retry() {
    setRetrying(true);
    try {
      await onRetry();
    } finally {
      setRetrying(false);
    }
  }

  return (
    <div
      role="status"
      className="flex flex-wrap items-center gap-3 rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm print:hidden"
    >
      <CloudOff className="h-4 w-4 shrink-0 text-red-500" aria-hidden />
      <p className="flex-1 min-w-[12rem]">
        {error.kind === "network"
          ? "Can't reach the server. You're still signed in — check your connection and try again."
          : "The server is having trouble right now. You're still signed in; some things may not work."}
      </p>
      <Button size="sm" variant="outline" onClick={retry} disabled={retrying}>
        {retrying ? "Retrying..." : "Retry"}
      </Button>
    </div>
  );
}
