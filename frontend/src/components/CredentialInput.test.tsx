import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { CredentialInput, type CredentialDraft } from "./secrets";

function Harness({ saved, error }: { saved: boolean; error?: string }) {
  const [draft, setDraft] = useState<CredentialDraft>({ value: "", replacing: false });
  return (
    <CredentialInput
      id="cred" label="GitHub token" help="Read-only where possible." createUrl="https://github.com/settings/personal-access-tokens"
      saved={saved} draft={draft} onChange={setDraft} error={error}
    />
  );
}

describe("CredentialInput", () => {
  it("asks for the credential by label, with help and a create link", () => {
    render(<Harness saved={false} />);
    const input = screen.getByLabelText("GitHub token");
    expect(input).toHaveAttribute("type", "password");
    expect(screen.getByText("Read-only where possible.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Create one/ })).toHaveAttribute("href", "https://github.com/settings/personal-access-tokens");
    expect(screen.queryByText(/GITHUB_TOKEN/)).toBeNull();
  });

  it("shows a value saved earlier and replaces it on request", async () => {
    const user = userEvent.setup();
    render(<Harness saved error="Enter the GitHub token." />);
    expect(screen.getByText("Saved earlier")).toBeInTheDocument();
    expect(screen.queryByText("Enter the GitHub token.")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Replace" }));
    expect(screen.getByLabelText("GitHub token")).toHaveValue("");
    expect(screen.getByText("Enter the GitHub token.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Keep saved" }));
    expect(screen.getByText("Saved earlier")).toBeInTheDocument();
  });
});
