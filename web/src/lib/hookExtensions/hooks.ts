import useSWR from "swr";
import { errorHandlingFetcher, skipRetryOnAuthError } from "@/lib/fetcher";
import { SWR_KEYS } from "@/lib/swr-keys";
import type {
  Hook,
  HookExecutionLog,
  HookPointMeta,
} from "@/lib/hookExtensions/types";

/** The API's largest `limit` for execution logs. */
export const HOOK_LOG_LIMIT = 100;

export function hookExecutionLogsKey(hookId: number): string {
  return `${SWR_KEYS.hooks}/${hookId}/execution-logs?limit=${HOOK_LOG_LIMIT}`;
}

/** Pass `enabled: false` to skip the fetch, e.g. when hooks are off. */
export function useHookSpecs(enabled: boolean) {
  return useSWR<HookPointMeta[]>(
    enabled ? SWR_KEYS.hookSpecs : null,
    errorHandlingFetcher,
    { onErrorRetry: skipRetryOnAuthError }
  );
}

export function useHooks(enabled: boolean) {
  return useSWR<Hook[]>(enabled ? SWR_KEYS.hooks : null, errorHandlingFetcher, {
    onErrorRetry: skipRetryOnAuthError,
  });
}

/** Pass null to skip the fetch until the logs are shown. */
export function useHookExecutionLogs(hookId: number | null) {
  return useSWR<HookExecutionLog[]>(
    hookId === null ? null : hookExecutionLogsKey(hookId),
    errorHandlingFetcher,
    { onErrorRetry: skipRetryOnAuthError }
  );
}
