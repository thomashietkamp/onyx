import { restrictProviderGroups } from "@/views/admin/LanguageModelsPage";

const groups = [
  { id: "add", title: "Add", providerNames: ["openai", "anthropic"] },
  {
    id: "gateways",
    title: "Gateways",
    providerNames: ["openrouter", "litellm_proxy"],
  },
  {
    id: "self",
    title: "Self-hosted",
    providerNames: ["ollama_chat"],
    includeCustom: true,
  },
];

describe("restrictProviderGroups", () => {
  it("shows every group when no allowlist is set", () => {
    expect(restrictProviderGroups(groups, [])).toEqual(groups);
    expect(restrictProviderGroups(groups, undefined)).toEqual(groups);
  });

  it("keeps only allowed providers and hides the custom cards", () => {
    expect(restrictProviderGroups(groups, ["litellm_proxy"])).toEqual([
      {
        id: "gateways",
        title: "Gateways",
        providerNames: ["litellm_proxy"],
        includeCustom: false,
      },
    ]);
  });
});
