import type { StandardAnswer, StandardAnswerCategory } from "@/lib/types";

export interface StandardAnswerInput {
  keyword: string;
  answer: string;
  categories: number[];
  match_regex: boolean;
  match_any_keywords: boolean;
}

async function request(path: string, init: RequestInit): Promise<Response> {
  const res = await fetch(`/api/manage/admin/standard-answer${path}`, {
    ...init,
    headers: { "Content-Type": "application/json" },
  });
  if (!res.ok) {
    const body: unknown = await res.json().catch(() => null);
    const detail =
      body && typeof body === "object" && "detail" in body
        ? String(body.detail)
        : res.statusText;
    throw new Error(detail);
  }
  return res;
}

async function send<T>(path: string, init: RequestInit): Promise<T> {
  return (await request(path, init)).json();
}

export function createStandardAnswer(
  input: StandardAnswerInput
): Promise<StandardAnswer> {
  return send("", { method: "POST", body: JSON.stringify(input) });
}

export function updateStandardAnswer(
  id: number,
  input: StandardAnswerInput
): Promise<StandardAnswer> {
  return send(`/${id}`, { method: "PATCH", body: JSON.stringify(input) });
}

export async function deleteStandardAnswer(id: number): Promise<void> {
  await request(`/${id}`, { method: "DELETE" });
}

export function createStandardAnswerCategory(
  name: string
): Promise<StandardAnswerCategory> {
  return send("/category", { method: "POST", body: JSON.stringify({ name }) });
}
