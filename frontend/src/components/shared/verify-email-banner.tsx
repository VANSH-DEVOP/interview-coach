"use client";

import { useState } from "react";
import { MailWarning } from "lucide-react";

import { api } from "@/lib/api-client";
import { useAuth } from "@/hooks/use-auth";
import { Button } from "@/components/ui/button";

/**
 * Prompts an unverified user to confirm their address.
 *
 * A prompt, not a gate. Nothing in the app is withheld from an unverified
 * account -- the default email backend writes to a log, so gating would make a
 * fresh local or demo deployment unusable. Dismissible for the same reason.
 */
export function VerifyEmailBanner() {
  const { user, isLoading } = useAuth();
  const [state, setState] = useState<"idle" | "sending" | "sent" | "failed">("idle");
  const [dismissed, setDismissed] = useState(false);

  if (isLoading || !user || user.email_verified || dismissed) return null;

  async function resend() {
    setState("sending");
    try {
      await api.post("/auth/resend-verification");
      setState("sent");
    } catch {
      setState("failed");
    }
  }

  return (
    <div
      role="status"
      className="flex flex-wrap items-center gap-3 rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm print:hidden"
    >
      <MailWarning className="h-4 w-4 shrink-0 text-amber-500" aria-hidden />
      <p className="flex-1 min-w-[12rem]">
        {state === "sent"
          ? `A new confirmation link is on its way to ${user.email}.`
          : state === "failed"
            ? "Couldn't send that just now. Try again in a moment."
            : `Confirm your email address (${user.email}) to secure your account.`}
      </p>
      {state !== "sent" && (
        <Button size="sm" variant="outline" onClick={resend} disabled={state === "sending"}>
          {state === "sending" ? "Sending..." : "Resend link"}
        </Button>
      )}
      <Button size="sm" variant="ghost" onClick={() => setDismissed(true)}>
        Dismiss
      </Button>
    </div>
  );
}
