"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";

import { api } from "@/lib/api-client";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

type State = "working" | "done" | "failed" | "missing";

function VerifyEmail() {
  const token = useSearchParams().get("token");
  const [state, setState] = useState<State>(token ? "working" : "missing");
  // The token is single-use, so a second submission always fails. In React's
  // development strict mode effects run twice, which would burn the token on
  // the first pass and report failure on the second.
  const attempted = useRef(false);

  useEffect(() => {
    if (!token || attempted.current) return;
    attempted.current = true;

    void api
      .post("/auth/verify-email", { token })
      .then(() => setState("done"))
      .catch(() => setState("failed"));
  }, [token]);

  if (state === "working") {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-xl">Confirming your email…</CardTitle>
        </CardHeader>
      </Card>
    );
  }

  if (state === "done") {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-xl">Email confirmed</CardTitle>
          <CardDescription>Thanks — your address is verified.</CardDescription>
        </CardHeader>
        <CardContent>
          <Link href="/dashboard">
            <Button className="w-full">Go to dashboard</Button>
          </Link>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-xl">
          {state === "missing" ? "This link is incomplete" : "This link no longer works"}
        </CardTitle>
        <CardDescription>
          {state === "missing"
            ? "The address is missing its token. Copy the whole link from the email."
            : "Confirmation links work once and expire after 24 hours. Sign in and send yourself a new one."}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <Link href="/login">
          <Button className="w-full">Sign in</Button>
        </Link>
      </CardContent>
    </Card>
  );
}

export default function VerifyEmailPage() {
  return (
    <Suspense fallback={<p className="text-sm text-muted-foreground">Loading…</p>}>
      <VerifyEmail />
    </Suspense>
  );
}
