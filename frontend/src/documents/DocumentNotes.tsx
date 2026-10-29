import { lazy, Suspense, useRef } from "react";
import { DocumentSourceLabel } from "./DocumentSourceLabel";
import { EmptyNotes, NotesPanel } from "../notes/NotesPanel";
import type { ReadState } from "../notes/useNoteResult";
import { apiUrl } from "../api";
import type { DocumentNotesResult, DocumentTask } from "../types";
import { ChapterNavigator } from "../notes/ChapterNavigator";
import { NoteMarkButton, useNoteMarks } from "../notes/NoteMarks";

const Markdown = lazy(() => import("../notes/NoteMarkdown"));

// 展示文档章节及来源位置，阅读和版本失败入口复用共用笔记面板。
export function DocumentNotes({ result, reading, versionReading, task, versions, select, returnToGeneration, jump }: {
  reading: ReadState; versionReading: ReadState;
  result: DocumentNotesResult | null; task: DocumentTask | null; versions: DocumentTask[];
  select: (id: string) => void; returnToGeneration: () => void;
  jump: (id: string) => void;
}) {
  const root = useRef<HTMLElement>(null);
  const chapters = result?.chapters || [];
  const concise = result?.note_style === "concise";
  const marks = useNoteMarks("document", task?.id, !!result?.complete && task?.status === "succeeded");
  return <NotesPanel id="document-notes" title="文档笔记" notesRef={root} style={result?.note_style}
    navigation={!concise && chapters.length >= 2 ? <ChapterNavigator chapters={chapters.map(c => ({ ...c, source_label: c.source_labels.join("；") }))}
      notesRef={root} anchorPrefix="document-note" /> : undefined}
    downloads={result && task ? { txt: apiUrl(`/document-analyses/${task.id}/notes?format=txt`),
      md: apiUrl(`/document-analyses/${task.id}/notes?format=md`) } : undefined}
    versions={{ ...versionReading, visible: true, selectedId: task?.id, latestId: versions[0]?.id,
      previousId: versions[1]?.id, select, returnToGeneration }} reading={reading}>
    {result?.complete === false && <p className="notes-partial" role="status">未完成笔记 · 已完成 {result.completed_segments}/{result.total_segments} 段 · 覆盖至 {result.covered_until_unit} 的第 {result.covered_until_offset} 字符</p>}
    {!concise && (result?.overview_paragraphs.length || task?.config.outline_preview) ? <div className="overview">
      <span className="eyebrow">材料导览</span>
      {result?.overview_paragraphs.length ? <><h3>{result.title}</h3>{result.overview_paragraphs.map((p, i) => <p key={i}>{p}</p>)}
        <ul className="summary-tags">{result.tags.map((tag, i) => <li key={i}>#{tag}</li>)}</ul></> : <p>{task?.config.outline_preview}</p>}
    </div> : null}
    {!result && !reading.loading && !reading.error && <EmptyNotes />}
    {concise && <h3 className="note-title">{result.title}</h3>}
    {marks.notice}
    <div className="timeline">{chapters.map(chapter => <article className="summary-section" id={`document-note-${chapter.chapter_id}`} tabIndex={-1} key={chapter.chapter_id}>
      <DocumentSourceLabel labels={chapter.source_labels} />
      {(!concise || result.total_segments > 1) && <div className="note-heading"><h3>{chapter.title}</h3>
        {!concise && <NoteMarkButton state={marks} chapter={chapter.chapter_id} />}</div>}
      <Suspense fallback={<p className="note-markdown-loading" role="status">正在显示正文…</p>}><Markdown
        onSourceJump={concise ? id => { if (chapter.source_ids.includes(id)) jump(id); } : undefined}
        headingAction={concise && marks.enabled ? index => <NoteMarkButton state={marks} chapter={chapter.chapter_id} heading={index} /> : undefined}
      >{chapter.markdown}</Markdown></Suspense>
    </article>)}</div>
    {!!result?.source_issues.length && <details className="note-source-issues"><summary>存疑部分</summary>
      {result.source_issues.map((issue, i) => <div key={i}>{issue.quote && <p>原文：{issue.quote}</p>}<p>{issue.reason}</p></div>)}
    </details>}
  </NotesPanel>;
}
