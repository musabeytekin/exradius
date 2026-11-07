import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { MissingKeysForm } from "./SecretsTab";

describe("missing keys form", () => {
  it("has a write-only input per missing key and saves the filled ones together", async () => {
    const onSave = vi.fn().mockResolvedValue(true);
    render(<MissingKeysForm keys={["DB_PASSWORD", "API_TOKEN", "SENTRY_DSN"]} onSave={onSave} />);
    const user = userEvent.setup();

    const save = screen.getByRole("button", { name: "Save all" });
    expect(save).toBeDisabled();
    const db = screen.getByLabelText("DB_PASSWORD");
    expect(db).toHaveAttribute("type", "password");
    expect(screen.getByLabelText("API_TOKEN")).toHaveAttribute("type", "password");

    await user.type(db, "pw");
    await user.type(screen.getByLabelText("SENTRY_DSN"), "https://x");
    expect(save).toBeEnabled();
    await user.click(save);

    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onSave).toHaveBeenCalledWith({ DB_PASSWORD: "pw", SENTRY_DSN: "https://x" });
    expect(db).toHaveValue("");
  });

  it("keeps the values when saving fails", async () => {
    const onSave = vi.fn().mockResolvedValue(false);
    render(<MissingKeysForm keys={["DB_PASSWORD"]} onSave={onSave} />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("DB_PASSWORD"), "pw");
    await user.click(screen.getByRole("button", { name: "Save all" }));
    expect(screen.getByLabelText("DB_PASSWORD")).toHaveValue("pw");
  });
});
