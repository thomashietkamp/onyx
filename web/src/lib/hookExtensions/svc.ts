import { fetchErrorFrom } from "@/lib/fetcher";
import { SWR_KEYS } from "@/lib/swr-keys";
import type {
  Hook,
  HookCreateRequest,
  HookUpdateRequest,
  HookValidateResponse,
} from "@/lib/hookExtensions/types";

/** Throws a `FetchError` that keeps the status and the JSON error body. */
async function send<T>(path: string, init: RequestInit): Promise<T> {
  const response = await fetch(`${SWR_KEYS.hooks}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json" },
  });
  if (!response.ok) {
    throw await fetchErrorFrom(response, response.statusText);
  }
  return response.json();
}

export function createHook(input: HookCreateRequest): Promise<Hook> {
  return send("", { method: "POST", body: JSON.stringify(input) });
}

export function updateHook(
  id: number,
  input: HookUpdateRequest
): Promise<Hook> {
  return send(`/${id}`, { method: "PATCH", body: JSON.stringify(input) });
}

export async function deleteHook(id: number): Promise<void> {
  await send<null>(`/${id}`, { method: "DELETE" });
}

/** The server checks the endpoint first and fails with 400 when it fails. */
export function activateHook(id: number): Promise<Hook> {
  return send(`/${id}/activate`, { method: "POST" });
}

export function deactivateHook(id: number): Promise<Hook> {
  return send(`/${id}/deactivate`, { method: "POST" });
}

export function validateHook(id: number): Promise<HookValidateResponse> {
  return send(`/${id}/validate`, { method: "POST" });
}
