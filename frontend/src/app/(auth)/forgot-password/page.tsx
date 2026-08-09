"use client";

import { useState, type FormEvent } from "react";
import Link from "next/link";

import { api } from "@/lib/api-client";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export default function ForgotPasswordPage() {
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [sent, setSent] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setIsSubmitting(true);

    const form = new FormData(event.currentTarget);
    try {
      await api.post("/auth/forgot-password", { email: String(form.get("email")) });
    } catch {
      // Swallowed on purpose. The server answers identically for addresses it
      // knows and addresses it does not, and showing an error here would undo
      // that: a visible failure is a signal about the address, which is exactly
      // what the endpoint refuses to give away.
    } finally {
      setIsSubmitting(false);
      setSent(true);
    }
  }

  if (sent) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-xl">Check your email</CardTitle>
          <CardDescription>
            If that address has an account, a reset link is on its way. It works once and
            expires in 30 minutes.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            Nothing arrived? Check your spam folder, or{" "}
            <button
              type="button"
              onClick={() => setSent(false)}
              className="text-primary hover:underline"
            >
              try another address
            </button>
            .
          </p>
          <p className="mt-6 text-center text-sm text-muted-foreground">
            <Link href="/login" className="text-primary hover:underline">
              Back to sign in
            </Link>
          </p>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-xl">Reset your password</CardTitle>
        <CardDescription>
          Enter your email address and we&apos;ll send you a link to set a new password.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSubmit} className="space-y-4" noValidate>
          <div className="space-y-2">
            <Label htmlFor="email">Email</Label>
            <Input id="email" name="email" type="email" autoComplete="email" required />
          </div>

          <Button type="submit" className="w-full" disabled={isSubmitting}>
            {isSubmitting ? "Sending..." : "Send reset link"}
          </Button>
        </form>

        <p className="mt-6 text-center text-sm text-muted-foreground">
          Remembered it?{" "}
          <Link href="/login" className="text-primary hover:underline">
            Sign in
          </Link>
        </p>
      </CardContent>
    </Card>
  );
}
