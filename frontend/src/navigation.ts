import { useCallback, useEffect, useState } from "react";

export type Mode = "home" | "notes" | "summary";
export type NoteCenterContext = { query: string; page: number; view: "search" | "marks" };
export type NoteTarget = { taskId: string; chapterId?: string; headingIndex?: number; returnTo: NoteCenterContext };
function centerParams(context: NoteCenterContext) {
  return { ...(context.view === "marks" ? { view: "marks" } : {}), q: context.query, page: String(context.page) };
}
// 统一保存笔记中心的查询、页码和视图，供分页及详情返回复用。
export function noteCenterLink(context: NoteCenterContext) {
  return "#notes?" + new URLSearchParams(centerParams(context));
}
// 将资料、任务和章节定位与返回上下文一起编码到既有 URL。
export function noteLocationLink(fileId: string, target: NoteTarget) {
  return `#summary/${encodeURIComponent(fileId)}?` + new URLSearchParams({ task: target.taskId,
    ...(target.chapterId ? { chapter: target.chapterId } : {}),
    ...(target.headingIndex ? { heading: String(target.headingIndex) } : {}), ...centerParams(target.returnTo) });
}
type PageRoute = { mode: Mode; fileId?: string; query?: string; page?: number; view?: "search" | "marks"; target?: NoteTarget };
export type Navigate = (mode: Mode, fileId?: string) => void;

// 解析历史兼容的哈希路由，并为缺失或非法的分页和定位参数设置默认值。
export function readRoute(hash: string): PageRoute {
  const [path, search = ""] = hash.split("?", 2);
  const params = new URLSearchParams(search);
  const summary = /^#summary(?:\/([^/]+))?$/.exec(path);
  const page = Number(params.get("page"));
  const heading = Number(params.get("heading"));
  const view = params.get("view") === "marks" ? "marks" : "search";
  if (path === "#notes") return { mode: "notes", query: params.get("q") || "", page: Number.isSafeInteger(page) && page > 0 ? page : 1, view };
  return summary
    ? { mode: "summary", fileId: summary[1], target: params.get("task") ? {
        taskId: params.get("task")!, chapterId: params.get("chapter") || undefined,
        headingIndex: Number.isSafeInteger(heading) && heading > 0 ? heading : undefined,
        returnTo: { query: params.get("q") || "", page: Number.isSafeInteger(page) && page > 0 ? page : 1, view },
      } : undefined }
    : { mode: "home" };
}
function routeHash(route: PageRoute) {
  if (route.mode === "notes") return noteCenterLink({ query: route.query || "", page: route.page || 1, view: route.view || "search" });
  if (route.target) return noteLocationLink(route.fileId!, route.target);
  return route.mode === "home"
    ? ""
    : route.mode + (route.fileId ? "/" + route.fileId : "");
}

// 设置作为覆盖层保留底下页面，关闭后恢复原资料和笔记定位参数。
export function usePageRoute() {
  const [route, setRoute] = useState(() => readRoute(location.hash));
  const [settingsOpen, setSettingsOpen] = useState(
    location.hash === "#settings",
  );
  useEffect(() => {
    const change = () => {
      setSettingsOpen(location.hash === "#settings");
      if (location.hash !== "#settings") setRoute(readRoute(location.hash));
    };
    window.addEventListener("hashchange", change);
    return () => window.removeEventListener("hashchange", change);
  }, []);
  const navigate: Navigate = useCallback((mode, fileId) => {
    const next = { mode, fileId };
    setRoute(next);
    setSettingsOpen(false);
    location.hash = routeHash(next);
  }, []);
  return {
    route,
    settingsOpen,
    navigate,
    closeSettings: () => { setSettingsOpen(false); location.hash = routeHash(route); },
  };
}
