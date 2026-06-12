"use client";

/** Authentication hook: login, register, logout, current-user state. */

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import { api, clearTokens, getAccessToken, setTokens } from "@/lib/api-client";
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

  const logout = useCallback(() => {
    clearTokens();
    setState({ user: null, isLoading: false });
    router.push("/login");
  }, [router]);

  return { ...state, login, register, logout, reload: loadUser };
}
