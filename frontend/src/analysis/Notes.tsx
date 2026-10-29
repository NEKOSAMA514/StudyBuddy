import { Fragment, lazy, Suspense, useRef, useState } from "react";
import type { Result, Task, Evidence, NoteChapter } from "../types";
import { EmptyNotes, NotesPanel } from "../notes/NotesPanel";
import type { ReadState } from "../notes/useNoteResult";
import { notesUrl } from "../api";
import { clock } from "../util/time";
import { EvidenceTable } from "./EvidenceTable";
import { ChapterNavigator } from "../notes/ChapterNavigator";
import { NoteMarkButton, useNoteMarks } from "../notes/NoteMarks";

const NoteMarkdown = lazy(() => import("../notes/NoteMarkdown"));

// 展示视频导览、分段正文与章级依据，复用版本控件和按需 Markdown 渲染。
export function Notes({
  result,
  reading,
  versionReading,
  evidenceReading,
  task,
  hasVersions,
  onVersionChange,
  latestVersionId,
  previousVersionId,
  onReturnToGeneration,
  processing,
  availableEvidence,
  jump,
}: {
  result: Result | null;
  reading: ReadState; versionReading: ReadState; evidenceReading: ReadState;
  task: Task | null;
  hasVersions: boolean;
  onVersionChange: (id: string) => void;
  latestVersionId?: string;
  previousVersionId?: string;
  onReturnToGeneration: () => void;
  processing: boolean;
  availableEvidence: Evidence[];
  jump: (seconds: number, noteId?: string) => void;
}) {
  const [openCitations, setOpenCitations] = useState<string[]>([]);
  const notesRef = useRef<HTMLElement>(null);
  const issues = result?.source_issues || [];
  const headings = result?.chapters || [];
  const concise = result?.note_style === "concise";
  const marks = useNoteMarks("video", task?.id, !!result && task?.status === "succeeded" && result.complete !== false);
  const guides = new Map(result?.segment_guides?.map((guide) => [guide.first_chapter_id, guide]));
  const finalOverview = result?.overview_paragraphs.length ? result : null;
  const preview = task?.config?.outline_preview;
  const overview = concise ? [] : finalOverview?.overview_paragraphs || (preview ? [preview] : []);
  function citation(block: NoteChapter) {
    const evidenceIds = new Set(block.evidence_ids);
    const open = openCitations.includes(block.chapter_id);
    const anchor = `note-block-${block.chapter_id}`;
    const panel = `block-evidence-${block.chapter_id}`;
    return (
      <>
        <button
          className="cite-button"
          title={`${evidenceIds.size} 条原始记录`}
          aria-expanded={open}
          aria-controls={panel}
          onClick={() =>
            setOpenCitations((previous) =>
              open
                ? previous.filter((id) => id !== block.chapter_id)
                : [...previous, block.chapter_id],
            )
          }
        >
          {open ? "收起本章依据" : "查看本章依据"}
        </button>
        {open && (
          <div id={panel}>
            {evidenceReading.loading && <p role="status">正在读取依据…</p>}
            {evidenceReading.error && <div className="reader-state" role="status"><span>{evidenceReading.error}</span>
              <button className="secondary" onClick={evidenceReading.retry}>重新读取依据</button></div>}
            <EvidenceTable
              transcriptSource={result?.recognition?.transcript_source || task?.config?.transcript_source}
              items={availableEvidence.filter((item) =>
                evidenceIds.has(item.id),
              )}
              onJump={(seconds) => jump(seconds, anchor)}
            />
          </div>
        )}
      </>
    );
  }
  return (
    <NotesPanel id="video-notes" title="视频笔记" notesRef={notesRef} style={result?.note_style}
      navigation={!concise && headings.length >= 2 ? <ChapterNavigator chapters={headings} notesRef={notesRef} /> : undefined}
      downloads={result && task ? { txt: notesUrl(task.id), md: notesUrl(task.id, "md") } : undefined}
      versions={{ ...versionReading, visible: hasVersions, selectedId: task?.id, latestId: latestVersionId,
        previousId: previousVersionId, select: onVersionChange, returnToGeneration: onReturnToGeneration }} reading={reading}>
      {result?.complete === false && (
        <div className="notes-partial" role="status">
          <strong>已完成 {result.completed_segments}/{result.total_segments} 段 · {concise ? `原记录已处理至第 ${result.source_cursor?.offset || 0} 字符` : `覆盖 00:00–${clock(result.covered_until_seconds || 0)}`}</strong>
          <p>{processing ? "后续内容正在生成，已完成内容可先阅读和下载。" : "已完成内容可继续阅读和下载。"}</p>
        </div>
      )}
      {!!overview.length && <div className="overview video-overview">
        <span className="eyebrow">材料导览</span>
        {finalOverview && <h3>{finalOverview.title}</h3>}
        {overview.map((paragraph, index) => (
          <p key={index}>{paragraph}</p>
        ))}
        {finalOverview && !!finalOverview.tags.length && (
          <ul className="summary-tags" aria-label="关键标签">
            {finalOverview.tags.map((tag, index) => (
              <li key={index}>#{tag}</li>
            ))}
          </ul>
        )}
      </div>}
      {marks.notice}
      {result ? (
        <>
          {concise && <h3 className="note-title">{result.title}</h3>}
          {!concise && <div className="section-label">
            按章节梳理 <span>{headings.length} 章</span>
          </div>}
          <div className="timeline">
            {headings.map((heading) => {
              const guide = guides.get(heading.chapter_id);
              return (
                <Fragment key={heading.chapter_id}>
                {guide && (
                  <aside className="overview segment-overview" aria-label={`第${guide.index}段导览`}>
                    <span className="eyebrow">第 {guide.index} 段导览 · {clock(guide.start_seconds)}–{clock(guide.end_seconds)}</span>
                    <p>{guide.summary}</p>
                  </aside>
                )}
                <article
                  className="summary-section"
                  id={`note-block-${heading.chapter_id}`}
                  tabIndex={-1}
                >
                  {heading.start_seconds !== null &&
                  heading.end_seconds !== null ? (
                    <button
                      className="time-button"
                      onClick={() =>
                        jump(
                          heading.start_seconds!,
                          `note-block-${heading.chapter_id}`,
                        )
                      }
                    >
                      {clock(heading.start_seconds)}–
                      {clock(heading.end_seconds)}
                      {heading.timestamp_basis === "frame" && (
                        <span className="timestamp-basis"> 画面采样</span>
                      )}
                    </button>
                  ) : (
                    <span className="timestamp-basis">暂无语音定位</span>
                  )}
                  {(!concise || (result.total_segments || 1) > 1) && <div className="note-heading"><h3>{heading.title}</h3>
                    {!concise && <NoteMarkButton state={marks} chapter={heading.chapter_id} />}</div>}
                  <div className="note-subsection">
                    <Suspense fallback={<p className="note-markdown-loading" role="status">正在显示正文…</p>}>
                        <NoteMarkdown onTimeJump={concise ? seconds => jump(seconds, `note-block-${heading.chapter_id}`) : undefined}
                          headingAction={concise && marks.enabled ? index => <NoteMarkButton state={marks} chapter={heading.chapter_id} heading={index} /> : undefined}>
                          {heading.markdown}
                        </NoteMarkdown>
                    </Suspense>
                    {!concise && citation(heading)}
                  </div>
                </article>
                </Fragment>
              );
            })}
          </div>
          {!!issues.length && (
            <aside className="note-source-issues" aria-label="存疑部分">
              <details>
                <summary>存疑部分：</summary>
                <ul>
                  {issues.map((issue) => (
                    <li key={issue.id}>
                      {issue.quote && <p>原文：{issue.quote}</p>}
                      <p>存疑原因：{issue.reason}</p>
                    </li>
                  ))}
                </ul>
              </details>
            </aside>
          )}
        </>
      ) : reading.loading || reading.error ? null : preview ? (
        <p className="muted">{processing ? "材料导览已生成，章节正文正在整理。" : "材料导览已保存，章节正文尚未完成，可继续或重试任务。"}</p>
      ) : (
        <EmptyNotes processing={processing} />
      )}
    </NotesPanel>
  );
}
