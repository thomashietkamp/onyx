import { FetchError, fetchErrorDetail } from "@/lib/fetcher";
import type {
  HookExecutionLog,
  HookValidateStatus,
} from "@/lib/hookExtensions/types";

/** The largest timeout the form accepts, in seconds. */
export const MAX_HOOK_TIMEOUT_SECONDS = 600;

const HOUR_MS = 60 * 60 * 1000;

/** The key under `admin.hooks.form.errors` for a failed save. */
export type HookFormErrorKey =
  | "connect"
  | "timeout"
  | "invalidApiKey"
  | "generic";

const CHECK_STATUS_PATTERN = /\((cannot_connect|timeout|auth_failed)\)/;

/** The endpoint check status in a 400 detail such as
 *  "The endpoint check failed (timeout): ...", or null when there is none. */
export function parseCheckStatus(detail: string): HookValidateStatus | null {
  const match = CHECK_STATUS_PATTERN.exec(detail);
  switch (match?.[1]) {
    case "cannot_connect":
      return "cannot_connect";
    case "timeout":
      return "timeout";
    case "auth_failed":
      return "auth_failed";
    default:
      return null;
  }
}

export function formErrorKeyForStatus(
  status: HookValidateStatus | null
): HookFormErrorKey {
  switch (status) {
    case "cannot_connect":
      return "connect";
    case "timeout":
      return "timeout";
    case "auth_failed":
      return "invalidApiKey";
    default:
      return "generic";
  }
}

/** Maps a failed create or update to the form error to show. */
export function formErrorKeyForError(error: unknown): HookFormErrorKey {
  if (!(error instanceof FetchError) || error.status !== 400) {
    return "generic";
  }
  const detail = fetchErrorDetail(error);
  return formErrorKeyForStatus(detail ? parseCheckStatus(detail) : null);
}

export interface GroupedHookLogs {
  pastHour: HookExecutionLog[];
  older: HookExecutionLog[];
}

/** Splits logs into the past hour (relative to `now`) and older ones,
 *  keeping their order. */
export function groupHookLogs(
  logs: HookExecutionLog[],
  now: number
): GroupedHookLogs {
  const cutoff = now - HOUR_MS;
  const pastHour: HookExecutionLog[] = [];
  const older: HookExecutionLog[] = [];
  for (const log of logs) {
    if (new Date(log.created_at).getTime() >= cutoff) {
      pastHour.push(log);
    } else {
      older.push(log);
    }
  }
  return { pastHour, older };
}

/** One plain-text line per log, for display, copy and download. */
export function formatHookLogLine(
  log: HookExecutionLog,
  unknownError: string
): string {
  const parts = [log.created_at];
  if (log.status_code !== null) parts.push(String(log.status_code));
  if (log.duration_ms !== null) parts.push(`${log.duration_ms}ms`);
  parts.push(log.error_message || unknownError);
  return parts.join(" ");
}

export function formatHookLogs(
  logs: HookExecutionLog[],
  unknownError: string
): string {
  return logs.map((log) => formatHookLogLine(log, unknownError)).join("\n");
}
