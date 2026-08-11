/**
 * API client behaviour, now that it holds no credentials.
 *
 * Token storage and refresh-and-retry used to be tested here. Both moved to the
 * BFF proxy when the session moved into httpOnly cookies, and their tests moved
 * with them — see `src/app/api/bff/route.test.ts`, which is where the
 * "an outage is not a logout" distinction is now pinned. What is left here is
 * the request shaping and the error taxonomy the whole UI branches on.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, NetworkError, api } from "./api-client";

/** Same-origin: the proxy this app serves, not the API directly. */
const API_URL = "/api/bff";

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

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
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

describe("requests", () => {
  it("prefixes the API url and parses JSON", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ id: 7 }));

    await expect(api.get<{ id: number }>("/things")).resolves.toEqual({ id: 7 });
    expect(callUrl(0)).toBe(`${API_URL}/things`);
  });

  it("never sends an Authorization header, signed in or not", async () => {
    // The inversion of the old contract. This module has no credential to
    // attach: the session is an httpOnly cookie the browser sends by itself and
    // this code cannot read. An Authorization header appearing here again would
    // mean a token had found its way back into JavaScript.
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

  it("makes exactly one request, whatever the status", async () => {
    // Retrying is the proxy's job now. A second attempt from here would double
    // every failed request and spend the AI rate limit twice.
    fetchMock.mockResolvedValueOnce(errorResponse(500, "internal_error"));

    await api.get("/things").catch(() => undefined);

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

// -- Refresh and retry ---------------------------------------------------------

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

  it("reads the proxy's upstream failure as transient, not as a logout", async () => {
    // When the proxy cannot reach the API it answers 503 rather than passing a
    // bare 401 through. That distinction is what stops an outage rendering as a
    // signed-out session -- the session-preserving half now lives in the
    // proxy's own tests.
    fetchMock.mockResolvedValueOnce(
      errorResponse(503, "upstream_unavailable", "Unable to reach the server."),
    );

    const error = await expectApiError(api.get("/things"));

    expect(error.isTransient).toBe(true);
    expect(error.kind).toBe("server");
  });
});
