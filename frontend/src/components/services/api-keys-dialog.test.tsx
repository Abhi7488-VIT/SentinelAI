import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NewKeyNotice } from "@/components/services/api-keys-dialog";
import type { ApiKeyCreated } from "@/lib/api/types";

afterEach(cleanup);

const created: ApiKeyCreated = {
  id: "k1",
  service_id: "s1",
  name: "collector",
  prefix: "sai_abcd",
  key: "sai_abcd1234_secretsecret",
  created_at: "2026-11-19T00:00:00Z",
  created_by: null,
  last_used_at: null,
  revoked_at: null,
  revoked_by: null,
};

describe("NewKeyNotice", () => {
  it("shows the plaintext once with a warning, copies it, and is dismissed", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const onDone = vi.fn();
    render(<NewKeyNotice created={created} onDone={onDone} />);
    expect(screen.getByRole("status").textContent).toContain("won't be shown again");
    expect(screen.getByTestId("new-api-key").textContent).toBe(created.key);
    await userEvent.click(screen.getByRole("button", { name: "Copy key" }));
    expect(writeText).toHaveBeenCalledWith(created.key);
    expect(await screen.findByRole("button", { name: "Copied" })).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(onDone).toHaveBeenCalledOnce();
  });
});
