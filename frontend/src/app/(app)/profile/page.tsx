"use client";

import { useState, type FormEvent } from "react";

import { api, ApiError, setTokens } from "@/lib/api-client";
import { useAuth } from "@/hooks/use-auth";
import type { TokenPair, User } from "@/types";
import { PageHeader } from "@/components/shared/page-header";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export default function ProfilePage() {
  const { user, isLoading, reload } = useAuth();
  const [message, setMessage] = useState<{ kind: "success" | "error"; text: string } | null>(null);
  const [isSaving, setIsSaving] = useState(false);
  const [passwordMessage, setPasswordMessage] = useState<{
    kind: "success" | "error";
    text: string;
  } | null>(null);
  const [isChangingPassword, setIsChangingPassword] = useState(false);

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

  async function handlePasswordChange(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPasswordMessage(null);

    const form = event.currentTarget;
    const data = new FormData(form);
    const newPassword = String(data.get("new_password"));

    // Checked here as well as on the server: a mistyped confirmation should not
    // cost a round trip that signs every other session out on success.
    if (newPassword !== String(data.get("confirm_password"))) {
      setPasswordMessage({ kind: "error", text: "The new passwords do not match." });
      return;
    }

    setIsChangingPassword(true);
    try {
      // The server revokes every session, including this one, and hands back a
      // replacement pair. Storing it is what keeps the user signed in here.
      const tokens = await api.post<TokenPair>("/users/me/password", {
        current_password: String(data.get("current_password")),
        new_password: newPassword,
      });
      setTokens(tokens);
      form.reset();
      setPasswordMessage({
        kind: "success",
        text: "Password changed. Any other devices have been signed out.",
      });
    } catch (err) {
      setPasswordMessage({
        kind: "error",
        text: err instanceof ApiError ? err.message : "Unable to change your password.",
      });
    } finally {
      setIsChangingPassword(false);
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

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Change password</CardTitle>
          <CardDescription>
            Changing your password signs you out on every other device.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handlePasswordChange} className="max-w-md space-y-4">
            <div className="space-y-2">
              <Label htmlFor="current_password">Current password</Label>
              <Input
                id="current_password"
                name="current_password"
                type="password"
                autoComplete="current-password"
                required
              />
            </div>
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

            {passwordMessage && (
              <p
                role="status"
                className={
                  passwordMessage.kind === "success"
                    ? "text-sm text-primary"
                    : "text-sm text-destructive"
                }
              >
                {passwordMessage.text}
              </p>
            )}

            <Button type="submit" disabled={isChangingPassword}>
              {isChangingPassword ? "Changing..." : "Change password"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </>
  );
}
