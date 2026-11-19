export type HookPoint =
  | "document_ingestion"
  | "document_push"
  | "query_processing";

export type HookFailStrategy = "hard" | "soft";

export type HookValidateStatus =
  | "passed"
  | "auth_failed"
  | "timeout"
  | "cannot_connect";

/** One hook point as `/api/admin/hooks/specs` describes it. */
export interface HookPointMeta {
  hook_point: HookPoint;
  display_name: string;
  description: string;
  docs_url: string | null;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>;
  default_timeout_seconds: number;
  default_fail_strategy: HookFailStrategy;
  fail_hard_description: string;
}

export interface Hook {
  id: number;
  name: string;
  hook_point: HookPoint;
  endpoint_url: string | null;
  api_key_masked: string | null;
  fail_strategy: HookFailStrategy;
  timeout_seconds: number;
  is_active: boolean;
  /** Null until the server has checked the endpoint. */
  is_reachable: boolean | null;
  creator_email: string | null;
  created_at: string;
  updated_at: string;
}

export interface HookCreateRequest {
  name: string;
  hook_point: HookPoint;
  endpoint_url: string;
  api_key?: string | null;
  fail_strategy?: HookFailStrategy | null;
  timeout_seconds?: number | null;
}

/** Omit `api_key` to keep the stored key. */
export interface HookUpdateRequest {
  name?: string;
  endpoint_url?: string;
  api_key?: string | null;
  fail_strategy?: HookFailStrategy;
  timeout_seconds?: number;
}

export interface HookValidateResponse {
  status: HookValidateStatus;
  error_message: string | null;
}

/** One failed hook call. The API returns failures only, newest first. */
export interface HookExecutionLog {
  error_message: string | null;
  status_code: number | null;
  duration_ms: number | null;
  created_at: string;
}
