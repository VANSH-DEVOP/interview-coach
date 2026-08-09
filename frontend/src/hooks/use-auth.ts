"use client";

/** Authentication hook: login, register, logout, current-user state. */

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import {
  api,
  clearTokens,
  getAccessToken,
  getRefreshToken,
  setTokens,
} from "@/lib/api-client";
import type { TokenPair, User } from "@/types";

interface AuthState {
  user: User | null;
  isLoading: boolean;
}

export function useAuth() {
  const router = useRouter();
  const [state, setState] = useState<AuthState>({ user: null, isLoading: true });

  const loadUser = useCallback(async () => {
    if (!getAccessToken()) {
      setState({ user: null, isLoading: false });
      return;
    }
    try {
      const user = await api.get<User>("/users/me");
      setState({ user, isLoading: false });
    } catch {
      setState({ user: null, isLoading: false });
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
      setState({ user: null, isLoading: false });
      router.push("/login");
    },
    [router]
  );

  return { ...state, login, register, logout, reload: loadUser };
}
