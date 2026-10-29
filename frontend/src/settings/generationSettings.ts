import { api, type RequestErrorPolicy } from "../api";
import type { NoteStyle } from "../types";

export type APIProfileSummary = { id: string; name: string; model: string; has_token: boolean };
// 并行读取当前默认风格与连接摘要，组合为任务确认表单的初始值。
export async function loadGenerationSettings(signal: AbortSignal, errorPolicy: RequestErrorPolicy = "automatic") {
  const [style, profiles] = await Promise.all([
    api<{ note_style: NoteStyle }>("/settings/note-style", { signal, errorPolicy }),
    api<{ active_id: string; profiles: APIProfileSummary[] }>("/settings/model/profiles", { signal, errorPolicy }),
  ]);
  return { style: style.note_style, ...profiles };
}
