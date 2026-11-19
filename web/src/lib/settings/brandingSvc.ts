import type { EnterpriseSettings } from "@/lib/settings/types";

export type LogoKind = "logo" | "logotype";

/** Write shape: every admin-set field. The logo flags follow the stored files. */
export type BrandingSettings = Omit<
  EnterpriseSettings,
  "use_custom_logo" | "use_custom_logotype"
>;

export interface BrandingLimits {
  field_max_lengths: Partial<Record<keyof BrandingSettings, number>>;
  max_logo_bytes: number;
  max_nav_items: number;
}

async function errorDetail(res: Response): Promise<string> {
  try {
    const body: unknown = await res.json();
    if (
      body &&
      typeof body === "object" &&
      "detail" in body &&
      typeof body.detail === "string"
    ) {
      return body.detail;
    }
  } catch {
    // Fall through to the status text.
  }
  return res.statusText;
}

async function send(
  path: string,
  init: RequestInit
): Promise<EnterpriseSettings> {
  const res = await fetch(`/api/admin/enterprise-settings${path}`, init);
  if (!res.ok) throw new Error(await errorDetail(res));
  return res.json();
}

/** Replaces the whole settings object, so pass every field to keep. */
export function saveBrandingSettings(
  settings: BrandingSettings
): Promise<EnterpriseSettings> {
  return send("", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(settings),
  });
}

export function uploadBrandingLogo(
  kind: LogoKind,
  file: File
): Promise<EnterpriseSettings> {
  const body = new FormData();
  body.append("file", file);
  return send(`/${kind}`, { method: "PUT", body });
}

export function deleteBrandingLogo(
  kind: LogoKind
): Promise<EnterpriseSettings> {
  return send(`/${kind}`, { method: "DELETE" });
}

/** The settings without the read-only logo flags. */
export function toBrandingSettings({
  use_custom_logo: _useCustomLogo,
  use_custom_logotype: _useCustomLogotype,
  ...settings
}: EnterpriseSettings): BrandingSettings {
  return settings;
}
