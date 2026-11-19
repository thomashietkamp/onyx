import { useTranslations } from "next-intl";
import type { HookPoint, HookPointMeta } from "@/lib/hookExtensions/types";

/** Each hook point's key under `admin.hooks.points`. */
const HOOK_POINT_KEYS = {
  document_ingestion: "documentIngestion",
  document_push: "documentPush",
  query_processing: "queryProcessing",
} as const satisfies Record<HookPoint, string>;

export interface HookPointText {
  name: string;
  description: string;
}

/** The translated name and description of a hook point. A point this build
 *  does not know falls back to the server's text. */
export function useHookPointText(): (meta: HookPointMeta) => HookPointText {
  const t = useTranslations("admin.hooks.points");
  return function hookPointText(meta) {
    if (!Object.hasOwn(HOOK_POINT_KEYS, meta.hook_point)) {
      return { name: meta.display_name, description: meta.description };
    }
    const key = HOOK_POINT_KEYS[meta.hook_point];
    return { name: t(`${key}.name`), description: t(`${key}.description`) };
  };
}
