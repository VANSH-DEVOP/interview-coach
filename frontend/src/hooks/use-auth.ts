"use client";

/** Authentication hook: login, register, logout, current-user state. */

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import {
  api,
  ApiError,
  clearTokens,
  getAccessToken,
  getRefreshToken,
  setTokens,
} from "@/lib/api-client";
import type { TokenPair, User } from "@/types";

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
    if (!getAccessToken()) {
      setState({ user: null, isLoading: false, connectionError: null });
      return;
    }
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
      const tokens = await api.post<TokenPair>("/auth/login", { email, password });
      setTokens(tokens);
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
      const tokens = await api.post<TokenPair>("/auth/login", { email, password });
      setTokens(tokens);
      await loadUser();
      router.push("/dashboard");
    },
    [loadUser, router]
  );

  const logout = useCallback(
    async (everywhere = false) => {
      const refreshToken = getRefreshToken();
      // Clearing the cookie only stops *this* browser using the token. Until
      // the server revokes it, a copy taken from the machine stays valid for
      // the full refresh lifetime.
      if (refreshToken) {
        try {
          await api.post("/auth/logout", {
            refresh_token: refreshToken,
            everywhere,
          });
        } catch {
          // Offline, or the token was already dead. Either way the user asked
          // to be signed out, so sign them out locally rather than trapping
          // them on the page with an error they cannot act on.
        }
      }
      clearTokens();
      setState({ user: null, isLoading: false, connectionError: null });
      router.push("/login");
    },
    [router]
  );

  return { ...state, login, register, logout, reload: loadUser };
}
