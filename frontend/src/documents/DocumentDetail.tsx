import { lazy, Suspense, useEffect, useState } from "react";
import type { DocumentFile, DocumentTask, DocumentNotesResult, TaskReceipt } from "../types";
import { taskStatusLabels, terminal } from "../shared/taskStatus";
import { useDocumentTask } from "./useDocumentTask";
import { useNoteVersions } from "../notes/useNoteVersions";
import { useNoteResult } from "../notes/useNoteResult";
import { useDocumentPreview } from "./useDocumentPreview";
import { DocumentNotes } from "./DocumentNotes";
import { DocumentSetupDialog } from "./DocumentSetupDialog";
import { DocumentImportDialog } from "./DocumentImportDialog";
import type { DocumentImportSelection } from "../types";
import { TaskActivity } from "../shared/TaskActivity";
import type { NoteTarget } from "../navigation";
import { useNoteTarget } from "../notes/useNoteTarget";
import { TaskProgress } from "../TaskProgress";
import { api, errorText } from "../api";
import { mineruHint, mineruTokenURL } from "../documents/mineru";

const Markdown = lazy(() => import("../notes/NoteMarkdown"));
const versionTime = (task: DocumentTask) => task.completed_at || task.updated_at;

// 将文档任务的有限阶段映射为共用进度展示结构。
function documentProgress(task: DocumentTask | null) {
  if (!task) return;
  const segmented = task.note_style !== "concise" && task.progress.total_segments > 1;
  const steps = [
    { key: "extracted", title: "提取文档资料", description: "按确认配置提取文字、表格，内嵌图片按需 OCR" },
    { key: "checking", title: "检查文档材料", description: "检查已提取的内容和本次生成配置" },
    ...(task.note_style === "concise" ? [
      { key: "concise", title: "生成简洁笔记", description: "按材料内容分段整理重点" },
    ] : segmented ? [
      { key: "segments", title: "逐段生成章节", description: "逐段规划章节、生成正文并关联文档来源" },
      { key: "finalizing", title: "整理与校验", description: "整理整体概览，校验并保存笔记" },
    ] : [
      { key: "planning", title: "规划文档章节", description: "梳理主题、核心问题与章节范围" },
      { key: "summarizing", title: "生成章节正文", description: "按章整理讲解，关联文档来源" },
      { key: "finalizing", title: "整理与校验", description: "整理整体概览，校验并保存笔记" },
    ]),
  ];
  const stage = task.stage.split(":")[0];
  const writing = segmented && ["planning", "summarizing"].includes(stage);
  const current = Math.max(1, steps.findIndex(step => step.key === (writing ? "segments" : stage)));
  const completed = task.status === "succeeded" ? steps.length : current;
  const detail = task.progress.segment > 0 && task.progress.total_segments > 0
    ? `第 ${task.progress.segment}/${task.progress.total_segments} 段${writing ? ` · ${task.progress.stage_labels[stage] || stage}` : ""}` : undefined;
  return { steps, completed_steps: completed, current_step: current + 1, percent: Math.round(completed / steps.length * 100), detail };
}

// 组合提取进度、笔记任务和原文预览，材料先就绪时也能进入详情阅读。
export function DocumentDetail({ document, onBack, onError, onTaskChanged, refresh, onBusyChange, settingsOpen, target: noteTarget,
  processDocument, processing, processingStage, processingError, extractionReconnecting, receipt, onTaken }: {
  document: DocumentFile;
  onBack: () => void;
  onError: (error: unknown) => void;
  onTaskChanged: (task: DocumentTask) => void;
  refresh: () => Promise<unknown>;
  onBusyChange: (busy: boolean) => void;
  settingsOpen: boolean;
  target?: NoteTarget;
  processDocument: (document: DocumentFile, selection: Partial<DocumentImportSelection>) => Promise<boolean>;
  processing: boolean;
  processingStage: string;
  processingError: string;
  extractionReconnecting?: boolean;
  receipt?: Pick<TaskReceipt, "task_id">;
  onTaken?: () => void;
}) {
  const analysis = useDocumentTask(document, onTaskChanged, refresh, onError, onBusyChange, receipt, onTaken);
  const versions = useNoteVersions({ scope: `document/${document.id}`, path: `/documents/${document.id}/analyses`, current: analysis.task, timestamp: versionTime, onError,
    requestedId: noteTarget?.taskId, navigationKey: JSON.stringify(noteTarget) });
  const reading = useNoteResult<DocumentNotesResult>({ identity: `document/${document.id}/${versions.task?.id || ""}`,
    path: `/document-analyses/${versions.task?.id || ""}`, revision: versions.task?.notes_revision || 0,
    enabled: !!versions.task && versions.task.notes_state !== "none", onError });
  const result = reading.result;
  const targetNotice = useNoteTarget(noteTarget, versions.task?.id, result, reading.loading || versions.loading,
    versions.requestedUnavailable, "document-note", "document-notes");
  const pending = document.extracted === false;
  const extraction = document.extraction;
  const offerToken = pending && document.format === "pdf" && (!extraction || extraction.parser === "mineru_agent");
  const reader = useDocumentPreview(document.id, versions.task?.id, onError, !pending);
  const { content, offset, target, setTarget, loading, preview, jump, expanded, setExpanded } = reader;
  const [setup, setSetup] = useState(false);
  const [needsToken, setNeedsToken] = useState(false);
  useEffect(() => {
    if (!offerToken || settingsOpen) { setNeedsToken(false); return; }
    const controller = new AbortController();
    api<{ has_token: boolean }>("/settings/mineru", { signal: controller.signal, errorPolicy: "silent" })
      .then(value => { if (!controller.signal.aborted) setNeedsToken(!value.has_token); }).catch(() => {});
    return () => controller.abort();
  }, [document.id, offerToken, settingsOpen]);
  useEffect(() => { if (settingsOpen) setSetup(false); }, [settingsOpen]);
  const current = analysis.receipt ? null : analysis.task;
  const progress = documentProgress(current);
  const extracting = !!extraction && ["queued", "running", "interrupted"].includes(extraction.status);
  function returnToGeneration() {
    if (current) versions.select(current.id);
    globalThis.document.getElementById("document-generation")?.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  return <div className="document-detail">
    {targetNotice}
    <button className="text-button detail-back" onClick={onBack}>← 返回</button>
    <section className="panel document-heading">
      <h1>{document.filename}</h1>
      <p>{document.format.toUpperCase()} · {pending ? extracting ? "原文件已保存，正在后台提取" : extraction?.status === "failed" ? "原文件已保存，提取未完成" : "原文件已保存，待开始处理" : `${document.unit_count} 个内容块 · ${document.character_count.toLocaleString()} 字符`}</p>
    </section>
    <section className="panel task-panel document-task" id="document-generation" aria-label="文档生成进度" tabIndex={-1}>
      <div className="panel-header">
        <h2>处理进度</h2>
        <span className={`status-pill ${current?.status || extraction?.status || ""}`}>{analysis.missing ? "无法同步" : analysis.receipt ? "同步中" : current ? taskStatusLabels[current.status] :
          extraction ? ({ queued: "排队中", running: "正在提取", failed: "处理失败", interrupted: "等待恢复", done: "提取完成" })[extraction.status] : "待开始"}</span>
      </div>
      {analysis.receipt && !analysis.missing && <p role="status">操作已接受，正在同步状态</p>}
      {analysis.missing && <p className="notice" role="alert">任务不存在，无法更新状态。<button className="secondary" onClick={analysis.retryRead}>重新读取任务</button></p>}
      {offerToken && needsToken && <p className="notice mineru-hint">{mineruHint}<a href={mineruTokenURL} target="_blank" rel="noopener noreferrer">免费申请 API Token ↗</a></p>}
      {processingError && <p className="notice" role="alert">{processingError}</p>}
      {extraction && <section className="task-activity" aria-label="文档提取进度">
        <h3>资料提取 · {extraction.parser === "local" ? "本地提取" : extraction.parser === "mineru_vlm" ? "MinerU 精准解析" : "MinerU 轻量解析"}</h3>
        {!Object.keys(extraction.progress).length && <p>排队中</p>}
        {Object.entries(extraction.progress).map(([key, value]) => <p key={key}><strong>{value.stage}</strong> · {value.message}</p>)}
        {extractionReconnecting && <p role="status">暂时无法更新状态，正在重新连接…</p>}
        {extraction.error && !processingError && !processing && <p className="notice" role="alert">{errorText(extraction.error)}</p>}
        {extraction.status === "failed" && <div className="settings-actions">
          <button disabled={processing} onClick={() => void processDocument(document, { parser: extraction.parser })}>重试未完成部分</button>
          {!extraction.document_id && extraction.parser !== "local" && <button disabled={processing}
            onClick={() => void processDocument(document, { parser: "local" })}>整份改用本地提取</button>}
        </div>}
      </section>}
      {(current || !extraction) && <TaskProgress progress={progress} status={current?.status} label="文档处理" detail={progress?.detail} />}
      {current && <>
        <p className="muted">已尝试 {current.request_count}/{current.request_limit} 次模型请求（含重试与修正）</p>
        <TaskActivity key={current.id} activity={current.activity} status={current.status} reconnecting={analysis.reconnecting}
          stage={current.progress.stage_labels[current.stage] || taskStatusLabels[current.status]}
          error={current.error} warnings={current.warnings} />
        {!!Object.keys(current.timings).length && <div className="timings">{Object.entries(current.timings).map(([key, seconds]) =>
          <span key={key}>{key === "total" ? "本次总耗时" : current.progress.stage_labels[key] || key} {seconds.toFixed(1)}s</span>)}</div>}
      </>}
      <div className="document-task-actions">
        {current && ["queued", "running"].includes(current.status) && <button className="secondary" disabled={analysis.busy || analysis.missing} onClick={() => void analysis.operate("stop")}>停止处理</button>}
        {current && ["paused", "interrupted"].includes(current.status) && <button className="secondary" disabled={analysis.busy || analysis.missing || current.request_count >= current.request_limit} onClick={() => void analysis.operate("resume")}>继续处理</button>}
        {current?.status === "failed" && <button className="secondary" disabled={analysis.busy || analysis.missing || current.request_count >= current.request_limit} onClick={() => void analysis.operate("retry")}>重试任务</button>}
        {(!current || terminal.has(current.status)) && !extracting && <button className="secondary" disabled={analysis.busy || !!analysis.receipt || analysis.missing || processing || !pending && !document.character_count} onClick={() => setSetup(true)}>
          {pending ? "开始处理" : current ? "重新生成笔记" : "生成笔记"}</button>}
        {current && current.notes_state !== "none" && <button className="secondary" onClick={() => {
          versions.select(current.id);
          requestAnimationFrame(() => globalThis.document.getElementById("document-notes")?.scrollIntoView({ behavior: "smooth", block: "start" }));
        }}>查看已生成笔记 ↓</button>}
      </div>
    </section>
    <DocumentNotes key={`document/${document.id}/${versions.task?.id || "empty"}`} result={result} reading={reading} versionReading={versions} task={versions.task} versions={versions.versions}
      select={versions.select} returnToGeneration={returnToGeneration} jump={reader.jumpSource} />
    {!pending && <section className="panel document-preview" aria-label="文档内容预览" ref={preview}>
      <details open={expanded} onToggle={event => setExpanded(event.currentTarget.open)}>
      <summary><h2>提取内容</h2></summary>
      {expanded && <>
      <div className="document-pagination">
        <form onSubmit={event => {
          event.preventDefault();
          const value = Number(target);
          if (Number.isInteger(value) && value >= 1 && value <= document.unit_count) jump(value - 1);
        }}>
          <label>内容块 <input type="number" min={1} max={Math.max(1, document.unit_count)} value={target}
            disabled={loading || !document.unit_count} onChange={event => setTarget(event.target.value)} /></label>
          <button className="secondary" disabled={loading || !document.unit_count}>定位</button>
        </form>
      </div>
      {loading ? <p role="status">正在读取文档…</p> : !content ? <button className="secondary" onClick={reader.retry}>重新读取</button> : <>
        {!content.total && <p>没有可预览的文字。</p>}
        {content.units.map(unit => <article className="document-unit" id={`document-unit-${unit.id}`} tabIndex={-1} key={unit.id}>
          <h3>{unit.label} <small>· {unit.id}</small></h3>
          {unit.text.trim() ? unit.format === "markdown"
            ? <Suspense fallback={<p>正在排版…</p>}><Markdown density="source">{unit.text}</Markdown></Suspense>
            : <pre className="document-text">{unit.text}</pre>
            : <p>此处没有可提取的文字，可能是空白页或图片内容。</p>}
        </article>)}
        <div className="document-pagination">
          <button className="secondary" disabled={offset === 0} onClick={() => jump(Math.max(0, offset - 10))}>上一组</button>
          <span>{content.total ? `${offset + 1}–${Math.min(offset + 10, content.total)} / ${content.total}` : "0 / 0"}</span>
          <button className="secondary" disabled={offset + 10 >= content.total} onClick={() => jump(offset + 10)}>下一组</button>
        </div>
      </>}
      </>}
      </details>
    </section>}
    {setup && (pending ? <DocumentImportDialog filename={document.filename} format={document.format} busy={processing} stage={processingStage} error={processingError}
      onClose={() => setSetup(false)} onError={onError} onStart={selection => processDocument(document, selection)} />
      : <DocumentSetupDialog id={document.id} busy={analysis.busy} onClose={() => setSetup(false)} onError={onError}
        onStart={(profile, style) => analysis.operate("start", profile, style)} />)}
  </div>;
}
