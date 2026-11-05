import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { providerFormModel, providerKinds } from "@/lib/providers";
import type { Provider, ProviderIn, ProviderKindSpec, Runtime } from "@/lib/types";
import { ProviderDialog } from "./ProviderForm";

const NOTICE = "Anthropic restricts subscription tokens to personal use of Claude Code; products built on the Agent SDK should use an API key.";

// The block GET /api/system/runtimes returns (docs/providers.md §2.1), plus a second kind with settings.
const RUNTIMES: Runtime[] = [
  {
    name: "claude_code", label: "Claude Code",
    providers: [
      {
        kind: "anthropic", label: "Anthropic",
        auth_methods: [
          { name: "api_key", label: "API key", credential_label: "API key", settings: [] },
          { name: "oauth_token", label: "Claude subscription token (claude setup-token)", credential_label: "OAuth token", settings: [], notice: NOTICE },
        ],
      },
      {
        kind: "anthropic_bedrock", label: "Amazon Bedrock",
        auth_methods: [
          {
            name: "access_key", label: "Access key", credential_label: "Secret access key",
            settings: [
              { name: "aws_region", label: "Region", required: true, options: ["us-east-1", "eu-central-1"] },
              { name: "aws_access_key_id", label: "Access key ID", required: true },
              { name: "base_url", required: false },
            ],
          },
        ],
      },
    ],
  },
];
const KINDS: ProviderKindSpec[] = providerKinds(RUNTIMES);

function setup(props: { provider?: Provider | null; takenSlugs?: string[] } = {}) {
  const onSave = vi.fn<(b: ProviderIn) => Promise<void>>().mockResolvedValue(undefined);
  render(<ProviderDialog open onOpenChange={() => {}} kinds={KINDS} provider={props.provider ?? null} takenSlugs={props.takenSlugs ?? []} onSave={onSave} />);
  return { onSave, user: userEvent.setup() };
}

async function pick(user: ReturnType<typeof userEvent.setup>, field: string, option: string) {
  await user.click(screen.getByRole("combobox", { name: field }));
  await user.click(await screen.findByRole("option", { name: option }));
}

describe("providerFormModel", () => {
  it("derives the options and fields from the capability block alone", () => {
    const m = providerFormModel(KINDS, null, null);
    expect(m.kinds.map((k) => k.value)).toEqual(["anthropic", "anthropic_bedrock"]);
    expect(m.authMethods.map((a) => a.value)).toEqual(["api_key", "oauth_token"]);
    expect(m.credentialLabel).toBe("API key");
    expect(m.notice).toBeNull();
    expect(providerFormModel(KINDS, "anthropic", "oauth_token").notice).toBe(NOTICE);
    const bedrock = providerFormModel(KINDS, "anthropic_bedrock", "api_key");
    // An auth method the kind does not offer falls back to its first one.
    expect(bedrock.method?.name).toBe("access_key");
    expect(bedrock.settings.map((f) => f.name)).toEqual(["aws_region", "aws_access_key_id", "base_url"]);
  });

  it("merges kinds declared by several runtimes", () => {
    const merged = providerKinds([...RUNTIMES, {
      name: "other", label: "Other",
      providers: [{ kind: "anthropic", label: "Anthropic", auth_methods: [{ name: "api_key", label: "dup", credential_label: "x", settings: [] }, { name: "extra", label: "Extra", credential_label: "Token", settings: [] }] }],
    }]);
    expect(merged.map((k) => k.kind)).toEqual(["anthropic", "anthropic_bedrock"]);
    expect(merged[0].auth_methods.map((m) => `${m.name}:${m.label}`)).toEqual(["api_key:API key", "oauth_token:Claude subscription token (claude setup-token)", "extra:Extra"]);
  });
});

describe("ProviderDialog", () => {
  it("preselects the first kind and method and labels the credential from it", () => {
    setup();
    expect(screen.getByRole("combobox", { name: "Kind" })).toHaveTextContent("Anthropic");
    expect(screen.getByRole("combobox", { name: "Authentication" })).toHaveTextContent("API key");
    expect(screen.getByLabelText("API key")).toHaveAttribute("type", "password");
    expect(screen.queryByText(NOTICE)).not.toBeInTheDocument();
    // Name and slug are derived from the kind.
    expect(screen.getByLabelText("Name")).toHaveValue("Anthropic");
    expect(screen.getByLabelText("Slug")).toHaveValue("anthropic");
    expect(screen.getByRole("button", { name: "Create and test" })).toBeDisabled();
  });

  it("shows the auth method's notice and relabels the credential when the method changes", async () => {
    const { user } = setup();
    await pick(user, "Authentication", "Claude subscription token (claude setup-token)");
    expect(screen.getByText(NOTICE)).toBeInTheDocument();
    expect(screen.getByLabelText("OAuth token")).toBeInTheDocument();
    expect(screen.queryByLabelText("API key")).not.toBeInTheDocument();
  });

  it("renders the settings a kind declares and requires the required ones", async () => {
    const { user, onSave } = setup({ takenSlugs: ["amazon-bedrock"] });
    await pick(user, "Kind", "Amazon Bedrock");
    expect(screen.getByRole("combobox", { name: "Authentication" })).toHaveTextContent("Access key");
    // Options become a pick list; others a text field; a label falls back to the name.
    expect(screen.getByRole("combobox", { name: "Region" })).toBeInTheDocument();
    expect(screen.getByLabelText("Access key ID")).toBeInTheDocument();
    expect(screen.getByLabelText(/Base url/)).toBeInTheDocument();
    // A slug that is taken gets a suffix.
    expect(screen.getByLabelText("Slug")).toHaveValue("amazon-bedrock-2");

    await user.type(screen.getByLabelText("Secret access key"), "s3cr3t");
    const submit = screen.getByRole("button", { name: "Create and test" });
    expect(submit).toBeDisabled();
    await pick(user, "Region", "eu-central-1");
    await user.type(screen.getByLabelText("Access key ID"), "AKIA123");
    expect(submit).toBeEnabled();
    await user.click(submit);
    expect(onSave).toHaveBeenCalledWith({
      name: "Amazon Bedrock", slug: "amazon-bedrock-2", kind: "anthropic_bedrock", auth_method: "access_key",
      credential: "s3cr3t", settings: { aws_region: "eu-central-1", aws_access_key_id: "AKIA123" },
    });
  });

  it("never shows a stored credential and keeps it unless the method changes", async () => {
    const provider: Provider = {
      id: "p1", slug: "anthropic", name: "Anthropic", kind: "anthropic", auth_method: "api_key",
      settings: {}, is_default: true, last_test: null, agent_count: 0,
    };
    const { user, onSave } = setup({ provider, takenSlugs: ["anthropic"] });
    const field = screen.getByLabelText("API key");
    expect(field).toHaveValue("");
    expect(screen.getByText(/Leave empty to keep it/)).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Name"));
    await user.type(screen.getByLabelText("Name"), "Production key");
    await user.click(screen.getByRole("button", { name: "Save and test" }));
    const body = onSave.mock.calls[0][0];
    expect(body).not.toHaveProperty("credential");
    expect(body.name).toBe("Production key");
    // Editing keeps the slug the provider already has.
    expect(body.slug).toBe("anthropic");

    // Switching the auth method needs a new credential.
    await pick(user, "Authentication", "Claude subscription token (claude setup-token)");
    expect(screen.getByRole("button", { name: "Save and test" })).toBeDisabled();
    await user.type(screen.getByLabelText("OAuth token"), "sk-ant-oat-x");
    expect(screen.getByRole("button", { name: "Save and test" })).toBeEnabled();
  });

  it("explains when no runtime offers a provider kind", () => {
    render(<ProviderDialog open onOpenChange={() => {}} kinds={[]} provider={null} takenSlugs={[]} onSave={vi.fn()} />);
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("No provider kinds offered")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Create and test" })).toBeDisabled();
  });
});
