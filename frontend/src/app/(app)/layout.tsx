"use client";

import { useAuth } from "@/hooks/use-auth";
import { Sidebar } from "@/components/layout/sidebar";
import { MobileNav } from "@/components/layout/mobile-nav";

export default function AppLayout({ children }: { children: React.ReactNode }) {
  const { logout } = useAuth();

  // Wrapped, not passed directly: these land on an onClick, which would hand
  // the click event to logout's `everywhere` parameter. A truthy event would
  // sign the user out of every device on an ordinary logout, and the types
  // would not complain -- `() => void` accepts a function with optional args.
  const handleLogout = () => void logout();

  return (
    <div className="flex min-h-screen flex-col lg:flex-row">
      <Sidebar onLogout={handleLogout} />
      <div className="flex flex-1 flex-col">
        <MobileNav onLogout={handleLogout} />
        <main className="flex-1 px-4 py-6 sm:px-6 lg:px-8">
          <div className="mx-auto w-full max-w-5xl space-y-6">{children}</div>
        </main>
      </div>
    </div>
  );
}
