/**
 * The banner that says "the server is unreachable" instead of letting the shell
 * imply the user has been signed out.
 *
 * Worth testing because the wording is the feature: it has to tell the user
 * they are still signed in, or they will try to fix it by signing in again.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ConnectionBanner } from "./connection-banner";
import { ApiError, NetworkError } from "@/lib/api-client";

describe("ConnectionBanner", () => {
  it("renders nothing when the connection is fine", () => {
    const { container } = render(<ConnectionBanner error={null} onRetry={vi.fn()} />);

    expect(container).toBeEmptyDOMElement();
  });

  it("says the server is unreachable, and that the session is intact", () => {
    render(<ConnectionBanner error={new NetworkError()} onRetry={vi.fn()} />);

    expect(screen.getByRole("status")).toHaveTextContent(/can't reach the server/i);
    expect(screen.getByRole("status")).toHaveTextContent(/still signed in/i);
  });

  it("distinguishes a broken server from an unreachable one", () => {
    render(
      <ConnectionBanner error={new ApiError(500, "internal_error", "x")} onRetry={vi.fn()} />
    );

    expect(screen.getByRole("status")).toHaveTextContent(/having trouble/i);
    expect(screen.getByRole("status")).toHaveTextContent(/still signed in/i);
  });

  it("retries through the callback", async () => {
    const onRetry = vi.fn();
    render(<ConnectionBanner error={new NetworkError()} onRetry={onRetry} />);

    screen.getByRole("button", { name: /retry/i }).click();

    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});
