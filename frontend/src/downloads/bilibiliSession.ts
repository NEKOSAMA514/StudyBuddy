import { api } from "../api";
import type { BilibiliSession } from "../types";

const eventName = "studybuddy-bilibili-session";
export const readBilibiliSession = (signal: AbortSignal) => api<BilibiliSession>("/bilibili/session", { signal, cache: "no-store" });
// 退出成功且请求仍有效时才广播新状态，失败不清空其他模块的登录状态。
export async function logoutBilibiliSession(signal: AbortSignal) {
  const session = await api<BilibiliSession>("/bilibili/session", { method: "DELETE", signal });
  signal.throwIfAborted();
  publishBilibiliSession(session);
  return session;
}
export function publishBilibiliSession(session: BilibiliSession) {
  window.dispatchEvent(new CustomEvent<BilibiliSession>(eventName, { detail: session }));
}
// 将登录设置与下载页面的会话同步集中在此处，不建立全局持久化缓存。
export function subscribeBilibiliSession(receive: (session: BilibiliSession) => void) {
  const listener = (event: Event) => receive((event as CustomEvent<BilibiliSession>).detail);
  window.addEventListener(eventName, listener);
  return () => window.removeEventListener(eventName, listener);
}
