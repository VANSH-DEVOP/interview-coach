"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

import { useAuth } from "@/hooks/use-auth";
import { Sidebar } from "@/components/layout/sidebar";
import { MobileNav } from "@/components/layout/mobile-nav";
import { ConnectionBanner } from "@/components/shared/connection-banner";
import { VerifyEmailBanner } from "@/components/shared/verify-email-banner";

export default function AppLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  // The banner reads this layout's own auth state rather than calling useAuth
  // itself: each call is a separate hook instance with its own /users/me
  // request, and the shell already has one.
  const { user, isLoading, logout, connectionError, reload } = useAuth();

  /**
   * The client-side half of route protection, and it is not redundant with
   * `src/middleware.ts`.
   *
   * Middleware only runs on a request to the server. A browser Back gesture
   * after signing out restores the page from the client router's cache without
   * one -- so the app shell rendered for a signed-out visitor, which is what
   * was reported. No data leaked, because every request behind it 401s, but
   * showing the shell at all is wrong.
   *
   * Three conditions, and the third is the one that is easy to omit:
   *
   *  - not still loading, or this fires before the first `/users/me` answers
   *    and bounces every visitor on arrival;
   *  - no user;
   *  - **no connection error.** A server that cannot be reached leaves `user`
   *    null, and redirecting on that would recreate exactly the bug the whole
   *    `connectionError` distinction exists to prevent: an outage rendering as
   *    a logout, with the login page equally unable to work.
   */
  const signedOut = !isLoading && !user && !connectionError;
  useEffect(() => {
    // `replace`, not `push`: the signed-out page must not become a Back target
    // of its own.
    if (signedOut) router.replace("/login");
  }, [signedOut, router]);

  // Wrapped, not passed directly: these land on an onClick, which would hand
  // the click event to logout's `everywhere` parameter. A truthy event would
  // sign the user out of every device on an ordinary logout, and the types
  // would not complain -- `() => void` accepts a function with optional args.
  const handleLogout = () => void logout();

  // Render nothing while the redirect is in flight. Returning the shell here
  // would show a signed-out visitor the navigation for a frame or two.
  if (signedOut) return null;

  return (
    <div className="flex min-h-screen flex-col lg:flex-row">
      <Sidebar onLogout={handleLogout} />
      <div className="flex flex-1 flex-col">
        <MobileNav onLogout={handleLogout} />
        <main className="flex-1 px-4 py-6 sm:px-6 lg:px-8">
          <div className="mx-auto w-full max-w-5xl space-y-6">
            <ConnectionBanner error={connectionError} onRetry={reload} />
            <VerifyEmailBanner />
            {children}
          </div>
        </main>
      </div>
    </div>
  );
}
