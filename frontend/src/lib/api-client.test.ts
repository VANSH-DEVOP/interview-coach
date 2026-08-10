/**
 * API client behaviour, especially the transparent refresh-and-retry.
 *
 * That path is the one users notice when it breaks: a 401 on an expired access
 * token should be invisible, and a genuinely dead session should log out
 * cleanly rather than looping. Neither is obvious from reading the code.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, NetworkError, api, clearTokens, getAccessToken, setTokens } from "./api-client";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api/v1";

/** A JSON Response, as fetch would return it. */
function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function errorResponse(status: number, code: string, message = "nope"): Response {
  return jsonResponse({ error: { code, message, details: null } }, status);
}

function noContent(): Response {
  return new Response(null, { status: 204 });
}

const TOKENS = {
  access_token: "access-1",
  refresh_token: "refresh-1",
  token_type: "bearer" as const,
};

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  clearTokens();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** The Request init of the nth fetch call. */
function callInit(index: number): RequestInit & { headers: Headers } {
  return fetchMock.mock.calls[index][1] as RequestInit & { headers: Headers };
}

function callUrl(index: number): string {
  return fetchMock.mock.calls[index][0] as string;
}

/** Assert a request rejects with an ApiError, and hand it back typed. */
async function expectApiError(promise: Promise<unknown>): Promise<ApiError> {
  try {
    await promise;
  } catch (error) {
    if (error instanceof ApiError) return error;
    throw error;
  }
  throw new Error("expected the request to reject, but it resolved");
}

// -- Tokens --------------------------------------------------------------------

describe("token storage", () => {
  it("round-trips the access token through cookies", () => {
    expect(getAccessToken()).toBeNull();
    setTokens(TOKENS);
    expect(getAccessToken()).toBe("access-1");
  });

  it("clears both tokens", () => {
    setTokens(TOKENS);
    clearTokens();
    expect(getAccessToken()).toBeNull();
  });

  it("survives a token value containing cookie-special characters", () => {
    setTokens({ ...TOKENS, access_token: "a;b=c d" });
    expect(getAccessToken()).toBe("a;b=c d");
  });
});

// -- Requests ------------------------------------------------------------------

describe("requests", () => {
  it("prefixes the API url and parses JSON", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ id: 7 }));

    await expect(api.get<{ id: number }>("/things")).resolves.toEqual({ id: 7 });
    expect(callUrl(0)).toBe(`${API_URL}/things`);
  });

  it("attaches the bearer token when one is stored", async () => {
    setTokens(TOKENS);
    fetchMock.mockResolvedValueOnce(jsonResponse({}));

    await api.get("/things");

    expect(callInit(0).headers.get("Authorization")).toBe("Bearer access-1");
  });

  it("sends no Authorization header when logged out", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({}));

    await api.get("/things");

    expect(callInit(0).headers.get("Authorization")).toBeNull();
  });

  it("sets a JSON content type for a body", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({}));

    await api.post("/things", { a: 1 });

    expect(callInit(0).headers.get("Content-Type")).toBe("application/json");
    expect(callInit(0).body).toBe(JSON.stringify({ a: 1 }));
  });

  it("lets the browser set the content type for FormData", async () => {
    // Setting it manually would omit the multipart boundary and the upload
    // would fail server-side.
    fetchMock.mockResolvedValueOnce(jsonResponse({}));
    const form = new FormData();
    form.append("file", new Blob(["x"]), "cv.pdf");

    await api.postForm("/resumes", form);

    expect(callInit(0).headers.get("Content-Type")).toBeNull();
  });

  it("sends PUT with a JSON body", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({}));

    await api.put("/interviews/1/answers", { content: "revised" });

    expect(callInit(0).method).toBe("PUT");
    expect(callInit(0).body).toBe(JSON.stringify({ content: "revised" }));
  });

  it("returns undefined for 204 rather than failing to parse an empty body", async () => {
    fetchMock.mockResolvedValueOnce(noContent());

    await expect(api.delete("/things/1")).resolves.toBeUndefined();
  });
});

// -- Errors --------------------------------------------------------------------

describe("errors", () => {
  it("parses the error envelope into an ApiError", async () => {
    fetchMock.mockResolvedValueOnce(errorResponse(409, "conflict", "Already exists."));

    const error = await expectApiError(api.get("/things"));

    expect(error.status).toBe(409);
    expect(error.code).toBe("conflict");
    expect(error.message).toBe("Already exists.");
  });

  it("degrades gracefully when the body is not the expected envelope", async () => {
    // A proxy or gateway can return HTML; the client must still throw an
    // ApiError rather than a JSON parse error.
    fetchMock.mockResolvedValueOnce(
      new Response("<html>502</html>", { status: 502, headers: { "Content-Type": "text/html" } }),
    );

    const error = await expectApiError(api.get("/things"));

    expect(error.status).toBe(502);
    expect(error.code).toBe("unknown_error");
  });

  it("does not attempt a refresh for non-401 errors", async () => {
    setTokens(TOKENS);
    fetchMock.mockResolvedValueOnce(errorResponse(500, "internal_error"));

    await api.get("/things").catch(() => undefined);

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

// -- Refresh and retry ---------------------------------------------------------

describe("refresh and retry", () => {
  it("refreshes on 401 and replays the original request", async () => {
    setTokens(TOKENS);
    fetchMock
      .mockResolvedValueOnce(errorResponse(401, "unauthorized")) // original
      .mockResolvedValueOnce(
        jsonResponse({ access_token: "access-2", refresh_token: "refresh-2", token_type: "bearer" }),
      ) // refresh
      .mockResolvedValueOnce(jsonResponse({ id: 7 })); // replay

    await expect(api.get<{ id: number }>("/things")).resolves.toEqual({ id: 7 });

    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(callUrl(1)).toBe(`${API_URL}/auth/refresh`);
    // The replay must carry the *new* token, or it 401s again.
    expect(callInit(2).headers.get("Authorization")).toBe("Bearer access-2");
    expect(getAccessToken()).toBe("access-2");
  });

  it("sends the stored refresh token to the refresh endpoint", async () => {
    setTokens(TOKENS);
    fetchMock
      .mockResolvedValueOnce(errorResponse(401, "unauthorized"))
      .mockResolvedValueOnce(
        jsonResponse({ access_token: "a2", refresh_token: "r2", token_type: "bearer" }),
      )
      .mockResolvedValueOnce(jsonResponse({}));

    await api.get("/things");

    expect(JSON.parse(callInit(1).body as string)).toEqual({ refresh_token: "refresh-1" });
  });

  it("clears tokens and rethrows when the refresh itself fails", async () => {
    setTokens(TOKENS);
    fetchMock
      .mockResolvedValueOnce(errorResponse(401, "unauthorized"))
      .mockResolvedValueOnce(errorResponse(401, "unauthorized")); // refresh rejected

    const error = await expectApiError(api.get("/things"));

    expect(error.status).toBe(401);
    expect(getAccessToken()).toBeNull();
  });

  it("retries at most once, so a persistently-401 endpoint cannot loop", async () => {
    setTokens(TOKENS);
    fetchMock
      .mockResolvedValueOnce(errorResponse(401, "unauthorized")) // original
      .mockResolvedValueOnce(
        jsonResponse({ access_token: "a2", refresh_token: "r2", token_type: "bearer" }),
      ) // refresh succeeds
      .mockResolvedValueOnce(errorResponse(401, "unauthorized")); // replay 401s too

    const error = await expectApiError(api.get("/things"));

    expect(error.status).toBe(401);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("does not try to refresh when there is no refresh token", async () => {
    // Logged out entirely: one request, no refresh attempt.
    fetchMock.mockResolvedValueOnce(errorResponse(401, "unauthorized"));

    await api.get("/things").catch(() => undefined);

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("replays a POST with its body intact", async () => {
    setTokens(TOKENS);
    fetchMock
      .mockResolvedValueOnce(errorResponse(401, "unauthorized"))
      .mockResolvedValueOnce(
        jsonResponse({ access_token: "a2", refresh_token: "r2", token_type: "bearer" }),
      )
      .mockResolvedValueOnce(jsonResponse({ ok: true }));

    await api.post("/interviews", { title: "Practice" });

    expect(callInit(2).method).toBe("POST");
    expect(callInit(2).body).toBe(JSON.stringify({ title: "Practice" }));
  });
});

// -- Telling an outage apart from a logout --------------------------------------
//
// The failure these cover is one the app used to get exactly backwards: a
// backend that was down, a dropped connection and a 500 were all reported as
// though the session had ended.

describe("unreachable server", () => {
  it("turns a fetch rejection into a showable ApiError", async () => {
    // fetch rejects only when no response arrives; a bare TypeError here is
    // neither an ApiError nor something a page can display.
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));

    const error = await expectApiError(api.get("/things"));

    expect(error).toBeInstanceOf(NetworkError);
    expect(error.kind).toBe("network");
    expect(error.message).toMatch(/unable to reach the server/i);
  });

  it("classifies statuses so callers can branch on the kind", () => {
    expect(new ApiError(401, "unauthorized", "x").kind).toBe("auth");
    expect(new ApiError(403, "forbidden", "x").kind).toBe("auth");
    expect(new ApiError(404, "not_found", "x").kind).toBe("client");
    expect(new ApiError(500, "internal_error", "x").kind).toBe("server");
    // Only the last two are worth retrying, and neither implicates the session.
    expect(new ApiError(500, "internal_error", "x").isTransient).toBe(true);
    expect(new NetworkError().isTransient).toBe(true);
    expect(new ApiError(401, "unauthorized", "x").isTransient).toBe(false);
  });

  it("keeps the session when the network drops mid-request", async () => {
    setTokens(TOKENS);
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));

    await api.get("/things").catch(() => undefined);

    // Signing the user out over a blip is what made an outage look like a logout.
    expect(getAccessToken()).toBe("access-1");
  });

  it("keeps the session when the refresh cannot be reached", async () => {
    setTokens(TOKENS);
    fetchMock
      .mockResolvedValueOnce(errorResponse(401, "unauthorized")) // access token expired
      .mockRejectedValueOnce(new TypeError("Failed to fetch")); // refresh unreachable

    const error = await expectApiError(api.get("/things"));

    // The refresh token may well still be valid -- nobody asked the server.
    expect(getAccessToken()).toBe("access-1");
    // And the user is told the truth rather than "unauthorized", which would
    // send them to a login page that cannot work either.
    expect(error.kind).toBe("network");
  });

  it("keeps the session when the refresh endpoint returns a 500", async () => {
    setTokens(TOKENS);
    fetchMock
      .mockResolvedValueOnce(errorResponse(401, "unauthorized"))
      .mockResolvedValueOnce(errorResponse(500, "internal_error"));

    const error = await expectApiError(api.get("/things"));

    expect(getAccessToken()).toBe("access-1");
    expect(error.kind).toBe("server");
  });

  it("still clears the session when the server actually rejects the refresh", async () => {
    // The other half of the contract: a genuinely dead session must not be
    // kept alive by the caution above.
    setTokens(TOKENS);
    fetchMock
      .mockResolvedValueOnce(errorResponse(401, "unauthorized"))
      .mockResolvedValueOnce(errorResponse(401, "token_revoked"));

    const error = await expectApiError(api.get("/things"));

    expect(getAccessToken()).toBeNull();
    expect(error.kind).toBe("auth");
  });
});
