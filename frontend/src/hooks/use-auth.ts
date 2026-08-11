"use client";

/** Authentication hook: login, register, logout, current-user state. */

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import { api, ApiError } from "@/lib/api-client";
import type { User } from "@/types";

interface AuthState {
  user: User | null;
  isLoading: boolean;
  /**
   * The server could not be asked who the user is. Not the same as being
   * signed out, and the UI must not present it as such.
   */
  connectionError: ApiError | null;
}

export function useAuth() {
  const router = useRouter();
  const [state, setState] = useState<AuthState>({
    user: null,
    isLoading: true,
    connectionError: null,
  });

  const loadUser = useCallback(async () => {
    // Asked unconditionally now. The session lives in an httpOnly cookie this
    // code cannot read, so "is anyone signed in" is a question only the server
    // can answer -- and a 401 answers it.
    try {
      const user = await api.get<User>("/users/me");
      setState({ user, isLoading: false, connectionError: null });
    } catch (error) {
      // The distinction this whole branch exists for. A backend that is down,
      // a dropped connection and a 500 all used to land in `user: null`, which
      // renders exactly like a logout -- so an outage looked as though the
      // session had ended, and signing in again was the obvious (useless)
      // response. Only an auth failure clears the user now; anything else
      // keeps whatever we last knew and reports the real problem.
      if (error instanceof ApiError && error.isTransient) {
        setState((previous) => ({
          user: previous.user,
          isLoading: false,
          connectionError: error,
        }));
        return;
      }
      setState({ user: null, isLoading: false, connectionError: null });
    }
  }, []);

  useEffect(() => {
    void loadUser();
  }, [loadUser]);

  const login = useCallback(
    async (email: string, password: string) => {
      // The proxy takes the token pair out of this response and puts it in
      // httpOnly cookies; nothing usable comes back to this code, by design.
      await api.post("/auth/login", { email, password });
      await loadUser();
      router.push("/dashboard");
    },
    [loadUser, router]
  );

  const register = useCallback(
    async (fullName: string, email: string, password: string) => {
      await api.post<User>("/auth/register", {
        full_name: fullName,
        email,
        password,
      });
      // Auto-login after successful registration.
      // The proxy takes the token pair out of this response and puts it in
      // httpOnly cookies; nothing usable comes back to this code, by design.
      await api.post("/auth/login", { email, password });
      await loadUser();
      router.push("/dashboard");
    },
    [loadUser, router]
  );

  const logout = useCallback(
    async (everywhere = false) => {
      try {
        // The proxy supplies the refresh token from its cookie and clears both
        // cookies afterwards -- this code has no token to send and no cookie it
        // could delete, since httpOnly is the point.
        //
        // Revoking server-side still matters: dropping a cookie only stops
        // *this* browser using the token, while a copy taken from the machine
        // would stay valid for the full refresh lifetime.
        await api.post("/auth/logout", { everywhere });
      } catch {
        // Offline, or the token was already dead. Either way the user asked to
        // be signed out, so sign them out locally rather than trapping them on
        // the page with an error they cannot act on.
      }
      setState({ user: null, isLoading: false, connectionError: null });
      router.push("/login");
    },
    [router]
  );

  return { ...state, login, register, logout, reload: loadUser };
}
