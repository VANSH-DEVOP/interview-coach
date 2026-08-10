/**
 * The hook that decides whether the app looks signed in.
 *
 * Everything here is about one distinction: `/users/me` failing because the
 * session ended, versus failing because the server could not be reached. The
 * hook used to treat them identically and empty the user either way, so an
 * outage rendered as a logout and the obvious response -- sign in again --
 * could not work.
 */

import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useAuth } from "./use-auth";
import { setTokens } from "@/lib/api-client";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
}));

const TOKENS = {
  access_token: "access-1",
  refresh_token: "refresh-1",
  token_type: "bearer" as const,
};

const USER = {
  id: "u1",
  email: "a@example.com",
  full_name: "A",
  email_verified: true,
  created_at: "2026-01-01T00:00:00",
};

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function errorResponse(status: number, code: string): Response {
  return jsonResponse({ error: { code, message: "nope", details: null } }, status);
}

describe("useAuth", () => {
  it("loads the current user when there is a session", async () => {
    setTokens(TOKENS);
    fetchMock.mockResolvedValueOnce(jsonResponse(USER));

    const { result } = renderHook(() => useAuth());

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.user).toMatchObject({ id: "u1" });
    expect(result.current.connectionError).toBeNull();
  });

  it("reports an unreachable server without signing the user out", async () => {
    setTokens(TOKENS);
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));

    const { result } = renderHook(() => useAuth());

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    // The whole point: a connection failure is surfaced as one, and the app
    // shell does not fall back to its signed-out rendering.
    expect(result.current.connectionError?.kind).toBe("network");
  });

  it("reports a broken backend the same way, not as a logout", async () => {
    setTokens(TOKENS);
    fetchMock.mockResolvedValue(errorResponse(500, "internal_error"));

    const { result } = renderHook(() => useAuth());

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.connectionError?.kind).toBe("server");
    expect(result.current.user).toBeNull();
  });

  it("does sign the user out when the session is genuinely over", async () => {
    // The other half: caution about outages must not keep a dead session alive.
    setTokens(TOKENS);
    fetchMock
      .mockResolvedValueOnce(errorResponse(401, "unauthorized")) // /users/me
      .mockResolvedValueOnce(errorResponse(401, "token_revoked")); // refresh refused

    const { result } = renderHook(() => useAuth());

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.user).toBeNull();
    expect(result.current.connectionError).toBeNull();
  });

  it("does not call the API at all without a token", async () => {
    const { result } = renderHook(() => useAuth());

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(fetchMock).not.toHaveBeenCalled();
    expect(result.current.user).toBeNull();
  });

  it("recovers on retry once the server comes back", async () => {
    setTokens(TOKENS);
    fetchMock
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(jsonResponse(USER));

    const { result } = renderHook(() => useAuth());
    await waitFor(() => expect(result.current.connectionError).not.toBeNull());

    await result.current.reload();

    await waitFor(() => expect(result.current.connectionError).toBeNull());
    expect(result.current.user).toMatchObject({ id: "u1" });
  });
});
