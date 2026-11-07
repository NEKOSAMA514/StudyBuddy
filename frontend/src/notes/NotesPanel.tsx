import { useCallback, useEffect, useId, useRef, type ReactNode, type RefObject } from "react";
import type { NoteStyle } from "../types";
import type { ReadState } from "./useNoteResult";

type NoteAction = { label: string; disabled?: boolean; onSelect: () => void };

function NoteActions({ actions }: { actions: NoteAction[] }) {
  const id = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const place = useCallback(() => {
    const popup = menu.current;
    const anchor = trigger.current?.getBoundingClientRect();
    if (!popup?.matches(":popover-open") || !anchor) return;
    if (anchor.bottom < 0 || anchor.top > window.innerHeight) {
      popup.hidePopover();
      return;
    }
    const below = anchor.bottom + 4;
    popup.style.left = `${Math.max(8, Math.min(anchor.left, window.innerWidth - popup.offsetWidth - 8))}px`;
    popup.style.top = `${below + popup.offsetHeight <= window.innerHeight ? below : Math.max(8, anchor.top - popup.offsetHeight - 4)}px`;
  }, []);
  useEffect(() => {
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [place]);
  return <>
    <button ref={trigger} type="button" className="file-menu-trigger" aria-label="笔记操作" popoverTarget={id}>⋯</button>
    <div id={id} ref={menu} className="file-action-menu note-action-menu" popover="auto" role="group" aria-label="笔记操作"
      onToggle={event => {
        if (event.newState !== "open") return;
        place();
        event.currentTarget.querySelector<HTMLButtonElement>("button:not([disabled])")?.focus({ preventScroll: true });
      }}>
      {actions.map(action => <button key={action.label} type="button" disabled={action.disabled} onClick={() => {
        menu.current!.hidePopover();
        action.onSelect();
      }}>{action.label}</button>)}
    </div>
  </>;
}

// 共用笔记标题、下载和版本控件，正文内容仍由视频与文档各自组织。
export function NotesPanel({ id, title, notesRef, style, navigation, downloads, actions, versions, reading, children }: {
  id: string; title: string; notesRef: RefObject<HTMLElement | null>; style?: NoteStyle;
  navigation?: ReactNode; downloads?: { txt: string; md: string };
  actions?: NoteAction[];
  versions: ReadState & { visible: boolean; selectedId?: string; latestId?: string; previousId?: string;
    select: (id: string) => void; returnToGeneration: () => void };
  reading: ReadState; children: ReactNode;
}) {
  return <section ref={notesRef} id={id} tabIndex={-1} data-note-style={style}
    className={`panel summary-panel${navigation ? " has-chapter-navigation" : ""}`}>
    {navigation}
    <div className="panel-header">
      <div className="notes-heading"><h2>{title}</h2>{!!actions?.length && <NoteActions actions={actions} />}</div>
      {downloads && <div className="notes-downloads">
        <a className="secondary notes-download" href={downloads.txt} download>下载 TXT</a>
        <a className="secondary notes-download" href={downloads.md} download>下载 Markdown</a>
      </div>}
    </div>
    {versions.visible && <div className="note-version-controls"><span className="note-version">笔记版本</span>
      <div className="note-version-actions" role="group" aria-label="笔记快捷操作">
        <button type="button" disabled={!versions.latestId} aria-pressed={!!versions.latestId && versions.selectedId === versions.latestId}
          onClick={() => versions.latestId && versions.select(versions.latestId)}>最新笔记</button>
        <button type="button" disabled={!versions.previousId} aria-pressed={!!versions.previousId && versions.selectedId === versions.previousId}
          onClick={() => versions.previousId && versions.select(versions.previousId)}>上一份笔记</button>
        <button type="button" onClick={versions.returnToGeneration}>回到生成</button>
      </div>
    </div>}
    {versions.loading && <p className="reader-state" role="status">正在读取版本列表…</p>}
    {versions.error && <div className="reader-state" role="status"><span>{versions.error}</span>
      <button type="button" className="secondary" onClick={versions.retry}>重新读取版本列表</button></div>}
    {reading.loading && <p className="reader-state" role="status">正在读取笔记…</p>}
    {reading.error && <div className="reader-state" role="status"><span>{reading.error}</span>
      <button type="button" className="secondary" onClick={reading.retry}>重新读取笔记</button></div>}
    {children}
  </section>;
}

// 区分尚无笔记与正在生成的空状态，不将空页面误认为读取失败。
export function EmptyNotes({ processing = false }: { processing?: boolean }) {
  return (
    <div className="empty-summary">
      <svg className="note-illustration" strokeWidth="1.5" viewBox="0 0 24 24" width="64" height="64" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
        <rect width="24" height="24" rx="6" fill="transparent" />
        <path d="M 5 4 L 15 4 L 19 8 L 19 21.5 L 5 21.5 Z" fill="none" stroke="currentColor" strokeLinejoin="round" />
        <path d="M 15 4 L 15 8 L 19 8" fill="none" stroke="currentColor" strokeLinejoin="round" />
        <line x1="8" y1="12" x2="16" y2="12" stroke="currentColor" strokeLinecap="round" />
        <line x1="8" y1="15" x2="16" y2="15" stroke="currentColor" strokeLinecap="round" />
        <line x1="8" y1="18" x2="13" y2="18" stroke="currentColor" strokeLinecap="round" />
      </svg>
      <h3>{processing ? "正在整理你的知识笔记" : "总结会出现在这里"}</h3>
      <p>
        整体概览、章节正文和原文依据，
        <br />
        在一处清晰呈现。
      </p>
      <div className="feature-list">
        <span>
          01 <strong>听讲解</strong> 识别语音与时间
        </span>
        <span>
          02 <strong>读画面</strong> 提取抽样画面文字
        </span>
        <span>
          03 <strong>做笔记</strong> 绑定真实来源依据
        </span>
      </div>
    </div>
  );
}
