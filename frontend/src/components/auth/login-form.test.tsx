import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LoginForm } from "@/components/auth/login-form";
import { ApiError } from "@/lib/api/client";

afterEach(cleanup);

describe("LoginForm", () => {
  it("validates before calling the API and ties errors to their fields", async () => {
    const onSubmit = vi.fn();
    render(<LoginForm onSubmit={onSubmit} />);
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(onSubmit).not.toHaveBeenCalled();
    const email = screen.getByLabelText("Email");
    expect(email.getAttribute("aria-invalid")).toBe("true");
    const describedBy = email.getAttribute("aria-describedby") ?? "";
    expect(document.getElementById(describedBy)?.textContent).toBe("Enter a valid email address.");
    expect(screen.getByText("Enter your password.")).toBeTruthy();
  });

  it("submits valid values and shows the server's message on failure", async () => {
    const onSubmit = vi.fn().mockRejectedValue(
      new ApiError(401, {
        error: { code: "invalid_credentials", message: "Invalid email or password.", request_id: "r1" },
      }),
    );
    render(<LoginForm onSubmit={onSubmit} />);
    await userEvent.type(screen.getByLabelText("Email"), "alice@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "wrong password");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(onSubmit).toHaveBeenCalledWith({ email: "alice@example.com", password: "wrong password" });
    expect((await screen.findByRole("alert")).textContent).toBe("Invalid email or password.");
  });

  it("explains rate limiting in plain words", async () => {
    const onSubmit = vi.fn().mockRejectedValue(
      new ApiError(429, { error: { code: "rate_limited", message: "Too many requests.", request_id: null } }),
    );
    render(<LoginForm onSubmit={onSubmit} />);
    await userEvent.type(screen.getByLabelText("Email"), "alice@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "x");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Too many attempts");
  });
});
