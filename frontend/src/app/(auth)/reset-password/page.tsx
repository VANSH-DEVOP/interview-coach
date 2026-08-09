"use client";

import { Suspense, useState, type FormEvent } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";

import { api, ApiError, setTokens } from "@/lib/api-client";
import type { TokenPair } from "@/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

function ResetPasswordForm() {
  const router = useRouter();
  const token = useSearchParams().get("token");
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);

    const form = new FormData(event.currentTarget);
    const password = String(form.get("new_password"));
    if (password !== String(form.get("confirm_password"))) {
      setError("The passwords do not match.");
      return;
    }

    setIsSubmitting(true);
    try {
      // The server signs every session out and hands back a fresh pair, so a
      // successful reset lands the user straight in the app rather than at a
      // login form asking for a password they invented four seconds ago.
      const tokens = await api.post<TokenPair>("/auth/reset-password", {
        token,
        new_password: password,
      });
      setTokens(tokens);
      router.push("/dashboard");
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : "Unable to reset your password. Request a new link."
      );
      setIsSubmitting(false);
    }
  }

  if (!token) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-xl">This link is incomplete</CardTitle>
          <CardDescription>
            The address is missing its reset token. Some email clients split long links across
            lines — copy the whole thing, or request a new one.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Link href="/forgot-password">
            <Button className="w-full">Request a new link</Button>
          </Link>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-xl">Choose a new password</CardTitle>
        <CardDescription>
          You&apos;ll be signed out everywhere else and signed in here.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSubmit} className="space-y-4" noValidate>
          <div className="space-y-2">
            <Label htmlFor="new_password">New password</Label>
            <Input
              id="new_password"
              name="new_password"
              type="password"
              autoComplete="new-password"
              minLength={8}
              required
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="confirm_password">Confirm new password</Label>
            <Input
              id="confirm_password"
              name="confirm_password"
              type="password"
              autoComplete="new-password"
              minLength={8}
              required
            />
          </div>

          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}

          <Button type="submit" className="w-full" disabled={isSubmitting}>
            {isSubmitting ? "Setting password..." : "Set new password"}
          </Button>
        </form>

        <p className="mt-6 text-center text-sm text-muted-foreground">
          Link expired?{" "}
          <Link href="/forgot-password" className="text-primary hover:underline">
            Request another
          </Link>
        </p>
      </CardContent>
    </Card>
  );
}

export default function ResetPasswordPage() {
  // useSearchParams opts the route into client rendering, which Next requires
  // a Suspense boundary for.
  return (
    <Suspense fallback={<p className="text-sm text-muted-foreground">Loading…</p>}>
      <ResetPasswordForm />
    </Suspense>
  );
}
