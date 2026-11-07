import { api, type RequestErrorPolicy } from "../api";
import type { NoteStyle } from "../types";

export type APIProfileSummary = { id: string; name: string; model: string; has_token: boolean };

export function loadAPIProfiles(signal: AbortSignal, errorPolicy: RequestErrorPolicy = "automatic") {
  return api<{ active_id: string; profiles: APIProfileSummary[] }>("/settings/model/profiles", { signal, errorPolicy });
}

// 并行读取当前默认风格与连接摘要，组合为任务确认表单的初始值。
export async function loadGenerationSettings(signal: AbortSignal, errorPolicy: RequestErrorPolicy = "automatic") {
  const [style, profiles] = await Promise.all([
    api<{ note_style: NoteStyle }>("/settings/note-style", { signal, errorPolicy }),
    loadAPIProfiles(signal, errorPolicy),
  ]);
  return { style: style.note_style, ...profiles };
}
