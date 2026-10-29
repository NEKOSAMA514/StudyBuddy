import { useEffect, useId, useRef, useState, type RefObject } from "react";
import { clock } from "../util/time";

type NavigableChapter = { chapter_id: string; title: string; start_seconds?: number; end_seconds?: number; source_label?: string };

// 按阅读位置高亮章节导航，悬停或键盘聚焦预览，点击后定位正文。
export function ChapterNavigator({ chapters, notesRef, anchorPrefix = "note-block" }: {
  chapters: NavigableChapter[];
  notesRef: RefObject<HTMLElement | null>;
  anchorPrefix?: string;
}) {
  const [visible, setVisible] = useState(false);
  const [active, setActive] = useState<string | null>(null);
  const [preview, setPreview] = useState<{ id: string; top: number } | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const pointerInside = useRef(false);
  const previewId = useId();

  useEffect(() => {
    if (!visible) {
      pointerInside.current = false;
      setPreview(null);
    }
  }, [visible]);

  useEffect(() => {
    const notes = notesRef.current;
    if (!notes || chapters.length < 2) return;
    const sections = chapters.map(chapter => document.getElementById(`${anchorPrefix}-${chapter.chapter_id}`));
    let frame = 0;
    function measure() {
      frame = 0;
      const bounds = notes!.getBoundingClientRect();
      setVisible(bounds.bottom > 80 && bounds.top < innerHeight);
      let current: string | null = null;
      sections.forEach((section, index) => {
        if (section && section.getBoundingClientRect().top <= 81) current = chapters[index].chapter_id;
      });
      setActive(current);
    }
    function schedule() {
      if (!frame) frame = requestAnimationFrame(measure);
    }
    const observer = new ResizeObserver(schedule);
    observer.observe(notes);
    sections.forEach(section => { if (section) observer.observe(section); });
    window.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", schedule);
    schedule();
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
      window.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", schedule);
    };
  }, [chapters, notesRef, anchorPrefix]);

  useEffect(() => {
    const rail = scroller.current;
    if (!rail || pointerInside.current || rail.contains(document.activeElement)) return;
    const mark = Array.from(rail.querySelectorAll<HTMLButtonElement>("button"))
      .find(button => button.dataset.chapter === active);
    if (!mark) return;
    const top = mark.offsetTop;
    if (top < rail.scrollTop || top + mark.offsetHeight > rail.scrollTop + rail.clientHeight) {
      rail.scrollTop = top - (rail.clientHeight - mark.offsetHeight) / 2;
    }
  }, [active, visible, chapters]);

  function showPreview(button: HTMLButtonElement, id: string) {
    setPreview({ id, top: button.getBoundingClientRect().top + button.offsetHeight / 2 });
  }
  function navigate(id: string) {
    const target = document.getElementById(`${anchorPrefix}-${id}`);
    target?.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
    target?.focus({ preventScroll: true });
    setPreview(null);
  }
  if (chapters.length < 2 || !visible) return null;
  const index = chapters.findIndex(chapter => chapter.chapter_id === preview?.id);
  const chapter = chapters[index];
  return <nav className="chapter-navigator" aria-label="笔记章节导航"
    onPointerEnter={() => { pointerInside.current = true; }}
    onPointerLeave={() => {
      pointerInside.current = false;
      if (!scroller.current?.contains(document.activeElement)) setPreview(null);
    }}>
    <div className="chapter-navigator-scroll" ref={scroller} onScroll={() => {
      const focused = document.activeElement;
      if (focused instanceof HTMLButtonElement && scroller.current?.contains(focused)) {
        showPreview(focused, focused.dataset.chapter!);
      } else setPreview(null);
    }}>
      {chapters.map((item, number) => <button key={item.chapter_id} type="button"
        className="chapter-mark" data-chapter={item.chapter_id}
        aria-label={`跳转到第 ${number + 1} 章：${item.title}`}
        aria-current={active === item.chapter_id ? "location" : undefined}
        aria-describedby={preview?.id === item.chapter_id ? previewId : undefined}
        onPointerMove={event => showPreview(event.currentTarget, item.chapter_id)}
        onFocus={event => showPreview(event.currentTarget, item.chapter_id)}
        onBlur={() => setPreview(null)} onClick={() => navigate(item.chapter_id)} />)}
    </div>
    {chapter && preview && <div id={previewId} role="tooltip" className="chapter-preview"
      style={{ top: `clamp(8px, ${preview.top - 40}px, calc(100dvh - 100px))` }}>
      <strong>第 {index + 1} 章 · {chapter.title}</strong>
      {typeof chapter.start_seconds === "number" && typeof chapter.end_seconds === "number" &&
        Number.isFinite(chapter.start_seconds) && Number.isFinite(chapter.end_seconds)
        ? <span>{clock(chapter.start_seconds)}–{clock(chapter.end_seconds)}</span>
        : chapter.source_label && <span>{chapter.source_label}</span>}
    </div>}
  </nav>;
}
