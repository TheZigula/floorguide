// client.ts — the only place the front end talks to the outside world.
// Owns: the base URL (from VITE_API_BASE alone), the four calls, and the choice of mock vs real backend.

import {
  ApiError,
  type ApproveRequest,
  type ApproveResponse,
  type ChatRequest,
  type ChatResponse,
  type HealthResponse,
  type RejectRequest,
  type RejectResponse,
} from "../types";
import { mockApprove, mockChat, mockHealth, mockReject } from "./mock";

/** Read once at module load. No key, no secret, no fallback host is ever baked in here. */
const API_BASE = (import.meta.env.VITE_API_BASE ?? "").trim().replace(/\/+$/, "");

/** True while VITE_API_BASE is unset: every call is answered by the scripted mock instead. */
export const USING_MOCK = API_BASE === "";

/** Shown in the header so nobody demos the mock believing it is the real backend. */
export const API_LABEL = USING_MOCK ? "mock data" : API_BASE;

async function post<TReq, TRes>(path: string, body: TReq): Promise<TRes> {
  let response: Response;
  try {
    response = await fetch(API_BASE + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "Could not reach FloorGuide at " + API_BASE + ". Is the backend running?");
  }
  if (!response.ok) {
    // FastAPI puts the reason in `detail`; a 409 from the approval gate arrives this way.
    const detail = await response
      .json()
      .then((payload: { detail?: string }) => payload.detail)
      .catch(() => undefined);
    throw new ApiError(response.status, detail ?? "The backend returned " + response.status + ".");
  }
  return (await response.json()) as TRes;
}

export function sendChat(req: ChatRequest): Promise<ChatResponse> {
  return USING_MOCK ? mockChat(req) : post<ChatRequest, ChatResponse>("/chat", req);
}

export function sendApprove(req: ApproveRequest): Promise<ApproveResponse> {
  return USING_MOCK ? mockApprove(req) : post<ApproveRequest, ApproveResponse>("/approve", req);
}

export function sendReject(req: RejectRequest): Promise<RejectResponse> {
  return USING_MOCK ? mockReject(req) : post<RejectRequest, RejectResponse>("/reject", req);
}

export async function getHealth(): Promise<HealthResponse> {
  if (USING_MOCK) return mockHealth();
  let response: Response;
  try {
    response = await fetch(API_BASE + "/health");
  } catch {
    throw new ApiError(0, "No answer from " + API_BASE + "/health.");
  }
  if (!response.ok) throw new ApiError(response.status, "/health returned " + response.status + ".");
  return (await response.json()) as HealthResponse;
}
