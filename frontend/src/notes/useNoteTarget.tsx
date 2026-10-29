import { useEffect, useRef, useState } from "react";
import { noteCenterLink, type NoteTarget } from "../navigation";

// 等待目标版本和延迟正文就绪后只定位一次，并保留返回笔记中心的上下文。
export function useNoteTarget(target: NoteTarget | undefined, taskId: string | undefined,
  result: { chapters: { chapter_id: string }[] } | null, loading: boolean, unavailable: boolean,
  prefix: string, rootId: string) {
  const key = target ? JSON.stringify(target) : "";
  const handled = useRef("");
  const [missing, setMissing] = useState("");
  useEffect(() => {
    if (!target) { handled.current = ""; return; }
    if (handled.current === key || taskId !== target.taskId || !result || loading) return;
    if (target.chapterId && !result.chapters.some(chapter => chapter.chapter_id === target.chapterId)) {
      setMissing(key); handled.current = key; return;
    }
    const root = document.getElementById(rootId);
    if (!root) return;
    const destination = target.chapterId ? `${prefix}-${target.chapterId}` : rootId;
    let frame = 0;
    function locate() {
      if (root!.querySelector(".timeline .note-markdown-loading, .note-marks-loading") || handled.current === key) return;
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const chapter = document.getElementById(destination);
        const element = target?.headingIndex ? chapter?.querySelector<HTMLElement>(`h2[data-note-heading="${target.headingIndex}"]`) : chapter;
        if (element) {
          element.scrollIntoView({ block: "start", behavior: "auto" });
          if (!element.hasAttribute("tabindex")) element.tabIndex = -1;
          element.focus({ preventScroll: true });
          handled.current = key;
          observer.disconnect();
        } else if (target?.headingIndex) {
          setMissing(key); handled.current = key; observer.disconnect();
        }
      });
    }
    // 结果到达后，延迟加载的正文和重点提示仍可能改变目标位置。
    const observer = new MutationObserver(locate);
    observer.observe(root, { childList: true, subtree: true });
    locate();
    return () => { cancelAnimationFrame(frame); observer.disconnect(); };
  }, [key, target, taskId, result, loading, prefix, rootId]);
  return target && <div className="notice">
    {(unavailable || missing === key) && <span role="status">{target.returnTo.view === "marks"
      ? "标记的笔记版本、章节或小节已不可用，请返回笔记中心刷新重点。"
      : "搜索命中的笔记版本或章节已不可用，请返回笔记中心重新搜索。"}</span>}
    {taskId === target.taskId && !result && !loading && <span role="status">{target.returnTo.view === "marks" ? "标记的笔记" : "搜索命中的笔记"}暂时无法读取。可重新读取笔记，或返回笔记中心。</span>}
    <a href={noteCenterLink(target.returnTo)}>← 返回笔记中心</a>
  </div>;
}
