/**
 * Pagination controls.
 *
 * The boundary behaviour is the part worth pinning: a disabled Previous on
 * page 1 and a disabled Next on the last page are what stop the UI requesting
 * page 0 or a page past the end.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Pagination } from "./pagination";

function setup(props: Partial<React.ComponentProps<typeof Pagination>> = {}) {
  const onPageChange = vi.fn();
  render(
    <Pagination page={1} size={10} total={35} onPageChange={onPageChange} {...props} />,
  );
  return { onPageChange };
}

describe("Pagination", () => {
  it("renders nothing when everything fits on one page", () => {
    const { container } = render(
      <Pagination page={1} size={20} total={12} onPageChange={vi.fn()} />,
    );
    // Callers drop it in unconditionally, so it has to be invisible here.
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when there are no records at all", () => {
    const { container } = render(
      <Pagination page={1} size={20} total={0} onPageChange={vi.fn()} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the range and page count", () => {
    setup({ page: 2, size: 10, total: 35 });

    expect(screen.getByText(/11–20 of 35/)).toBeInTheDocument();
    expect(screen.getByText(/Page 2 of 4/)).toBeInTheDocument();
  });

  it("clamps the displayed range on a partial last page", () => {
    setup({ page: 4, size: 10, total: 35 });

    // 31-35, not 31-40.
    expect(screen.getByText(/31–35 of 35/)).toBeInTheDocument();
  });

  it("disables Previous on the first page", () => {
    setup({ page: 1 });

    expect(screen.getByLabelText("Previous page")).toBeDisabled();
    expect(screen.getByLabelText("Next page")).toBeEnabled();
  });

  it("disables Next on the last page", () => {
    setup({ page: 4, size: 10, total: 35 });

    expect(screen.getByLabelText("Next page")).toBeDisabled();
    expect(screen.getByLabelText("Previous page")).toBeEnabled();
  });

  it("requests the neighbouring pages", async () => {
    const { onPageChange } = setup({ page: 2 });

    screen.getByLabelText("Next page").click();
    expect(onPageChange).toHaveBeenCalledWith(3);

    screen.getByLabelText("Previous page").click();
    expect(onPageChange).toHaveBeenCalledWith(1);
  });

  it("uses the label in its accessible name", () => {
    setup({ label: "interviews" });

    expect(screen.getByRole("navigation", { name: /interviews pagination/i })).toBeInTheDocument();
  });
});
