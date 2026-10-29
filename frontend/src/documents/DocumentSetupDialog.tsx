import { APIProfileSelect } from "../settings/APIProfileSelect";
import { loadGenerationSettings, type APIProfileSummary } from "../settings/generationSettings";
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { DocumentAnalysisPreview, NoteStyle } from "../types";
import { NoteStyleSelect } from "../settings/NoteStyleSelect";

// 已有材料重新生成前读取配置并预检规模，不再次提取原文件。
export function DocumentSetupDialog({ id, busy, onStart, onClose, onError }: {
  id: string; busy: boolean; onStart: (profileId: string, noteStyle: NoteStyle) => Promise<boolean>;
  onClose: () => void; onError: (error: unknown) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [data, setData] = useState<{ preview: DocumentAnalysisPreview; profiles: APIProfileSummary[] } | null>(null);
  const [selected, setSelected] = useState("");
  const [noteStyle, setNoteStyle] = useState<NoteStyle>("detailed");
  const [previewKey, setPreviewKey] = useState("");
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const element = dialog.current!;
    element.showModal();
    const controller = new AbortController();
    loadGenerationSettings(controller.signal, "caller")
      .then(async (profiles) => {
        const preview = await api<DocumentAnalysisPreview>(`/documents/${id}/analysis-preview?llm_profile_id=${encodeURIComponent(profiles.active_id)}&note_style=${profiles.style}`, { errorPolicy: "caller", signal: controller.signal });
        if (!controller.signal.aborted) { setData({ preview, profiles: profiles.profiles }); setSelected(profiles.active_id);
          setNoteStyle(profiles.style); setPreviewKey(`${profiles.active_id}:${profiles.style}`); }
      }).catch(error => { if (!controller.signal.aborted) { setFailed(true); onError(error); } });
    return () => { controller.abort(); element.close(); };
  }, [id, onError]);
  useEffect(() => {
    if (!selected || `${selected}:${noteStyle}` === previewKey) return;
    const controller = new AbortController();
    setFailed(false);
    api<DocumentAnalysisPreview>(`/documents/${id}/analysis-preview?llm_profile_id=${encodeURIComponent(selected)}&note_style=${noteStyle}`, { errorPolicy: "caller", signal: controller.signal })
      .then(preview => {
        if (!controller.signal.aborted) { setData(previous => previous && { ...previous, preview }); setPreviewKey(`${selected}:${noteStyle}`); }
      }).catch(error => { if (!controller.signal.aborted) { setFailed(true); onError(error); } });
    return () => controller.abort();
  }, [selected, noteStyle, previewKey, id, onError]);
  const previewReady = `${selected}:${noteStyle}` === previewKey;
  return <dialog ref={dialog} className="settings-dialog analysis-setup-dialog" aria-labelledby="document-setup-title"
    onCancel={event => { event.preventDefault(); if (!busy) onClose(); }}>
    <button className="dialog-close" aria-label="关闭文档生成设置" disabled={busy} onClick={onClose}>×</button>
    <h2 id="document-setup-title">生成文档笔记</h2>
    {!data ? <p role="status">{failed ? "读取失败，请关闭后重试。" : "正在检查材料…"}</p> : <>
      {previewReady ? <p>{data.preview.character_count.toLocaleString()} 字符 · 预计 {data.preview.estimated_segments} 段</p>
        : <p role="status">{failed ? "检查失败，请重新选择配置或关闭后重试。" : "正在检查所选配置的材料规模…"}</p>}
      <p className="muted">最多 {data.preview.limits.characters.toLocaleString()} 字符、{data.preview.limits.segments} 段、{data.preview.limits.requests} 次请求（含重试）。文档和视频按提交顺序排队；片段数不是费用或耗时估价。</p>
      {previewReady && !data.preview.eligible && <p className="notice">{data.preview.reason}</p>}
      <APIProfileSelect profiles={data.profiles} value={selected} disabled={busy} onChange={setSelected} />
      <NoteStyleSelect value={noteStyle} disabled={busy} onChange={setNoteStyle} />
      {!data.profiles.find(p => p.id === selected)?.has_token && <p>请先在设置中为此配置填写 API 密钥。</p>}
      <p className="muted">沿用已有提取内容和当前已保存的笔记补充要求，不再次 OCR。如需换用 OCR 模型，请重新导入原文件；图片识别结果以提取预览为准。</p>
    </>}
    <div className="settings-actions">
      <button disabled={busy} onClick={onClose}>取消</button>
      <button className="primary" disabled={busy || !previewReady || !data?.preview.eligible || !data?.profiles.find(p => p.id === selected)?.has_token}
        onClick={async () => { if (await onStart(selected, noteStyle)) onClose(); }}>{busy ? "正在提交…" : "开始生成"}</button>
    </div>
  </dialog>;
}
