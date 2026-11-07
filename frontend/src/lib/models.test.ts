import { describe, expect, it } from "vitest";
import { ANTHROPIC_MODELS, OTHER, RUNTIME_DEFAULT, modelLabel, modelSelection, modelValue, modelsFor } from "./models";

describe("model select", () => {
  it("uses the provider catalog's models, accepting bare ids", () => {
    const kind = { kind: "bedrock", models: [{ id: "anthropic.claude-x", description: "X" }, "anthropic.claude-y"] };
    expect(modelsFor(kind).map((m) => m.id)).toEqual(["anthropic.claude-x", "anthropic.claude-y"]);
  });

  it("falls back to the static list for anthropic only when the API sends none", () => {
    expect(modelsFor({ kind: "anthropic" })).toBe(ANTHROPIC_MODELS);
    expect(modelsFor({ kind: "anthropic", models: [] })).toBe(ANTHROPIC_MODELS);
    expect(modelsFor(undefined, "anthropic")).toBe(ANTHROPIC_MODELS);
    expect(modelsFor({ kind: "vertex" })).toEqual([]);
    expect(ANTHROPIC_MODELS.map((m) => m.id)).toEqual(["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]);
  });

  it("selects runtime default, a listed model, or Other for an unknown id", () => {
    expect(modelSelection(null, ANTHROPIC_MODELS)).toBe(RUNTIME_DEFAULT);
    expect(modelSelection("claude-sonnet-5-5", ANTHROPIC_MODELS)).toBe("claude-sonnet-5-5");
    expect(modelSelection("claude-next-preview", ANTHROPIC_MODELS)).toBe(OTHER);
  });

  it("saves null for the runtime default and the typed id for Other", () => {
    expect(modelValue(RUNTIME_DEFAULT, "ignored")).toBeNull();
    expect(modelValue("claude-opus-5-5", "")).toBe("claude-opus-5-5");
    expect(modelValue(OTHER, "  claude-next-preview ")).toBe("claude-next-preview");
    expect(modelValue(OTHER, "  ")).toBeNull();
  });

  it("labels models", () => {
    expect(modelLabel(null)).toBe("Runtime default");
    expect(modelLabel("claude-opus-5-5")).toBe("Claude Opus 5.5");
    expect(modelLabel("custom-id")).toBe("custom-id");
  });
});
