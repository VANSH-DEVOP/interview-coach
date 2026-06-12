"use client";

import { useState, type FormEvent } from "react";

import { api, ApiError } from "@/lib/api-client";
import { useAuth } from "@/hooks/use-auth";
import type { User } from "@/types";
import { PageHeader } from "@/components/shared/page-header";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export default function ProfilePage() {
  const { user, isLoading, reload } = useAuth();
  const [message, setMessage] = useState<{ kind: "success" | "error"; text: string } | null>(null);
  const [isSaving, setIsSaving] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setMessage(null);
    setIsSaving(true);

    const form = new FormData(event.currentTarget);
    try {
      await api.patch<User>("/users/me", { full_name: String(form.get("full_name")) });
      await reload();
      setMessage({ kind: "success", text: "Profile updated." });
    } catch (err) {
      setMessage({
        kind: "error",
        text: err instanceof ApiError ? err.message : "Unable to update profile.",
      });
    } finally {
      setIsSaving(false);
    }
  }

  return (
    <>
      <PageHeader title="Profile" description="Manage your account information." />

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Account details</CardTitle>
          <CardDescription>Your email address cannot be changed.</CardDescription>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <p className="text-sm text-muted-foreground">Loading…</p>
          ) : (
            <form onSubmit={handleSubmit} className="max-w-md space-y-4">
              <div className="space-y-2">
                <Label htmlFor="email">Email</Label>
                <Input id="email" value={user?.email ?? ""} disabled />
              </div>
              <div className="space-y-2">
                <Label htmlFor="full_name">Full name</Label>
                <Input
                  id="full_name"
                  name="full_name"
                  defaultValue={user?.full_name ?? ""}
                  required
                />
              </div>

              {message && (
                <p
                  role="status"
                  className={
                    message.kind === "success" ? "text-sm text-primary" : "text-sm text-destructive"
                  }
                >
                  {message.text}
                </p>
              )}

              <Button type="submit" disabled={isSaving}>
                {isSaving ? "Saving..." : "Save changes"}
              </Button>
            </form>
          )}
        </CardContent>
      </Card>
    </>
  );
}
