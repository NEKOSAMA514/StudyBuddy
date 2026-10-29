// 文件和本次配置为主，OCR就绪状态为次；沿用现有弹窗、表单和主题，仅“开始生成”为主操作。
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { APIProfileSelect } from "../settings/APIProfileSelect";
import { loadGenerationSettings } from "../settings/generationSettings";
import { NoteStyleSelect } from "../settings/NoteStyleSelect";
import type { RecognitionCatalog, DocumentImportSelection } from "../types";


// 确认本次解析、OCR、API 和笔记风格，取消只关闭表单并保留原件。
export function DocumentImportDialog({ filename, format, busy, stage, error, onStart, onClose, onError }: {
  filename: string; format: string; busy: boolean; stage: string; error: string;
  onStart: (selection: DocumentImportSelection) => Promise<boolean>;
  onClose: () => void; onError: (error: unknown) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [data, setData] = useState<{ recognition: RecognitionCatalog; generation: Awaited<ReturnType<typeof loadGenerationSettings>>; mineru: { has_token: boolean } | null } | null>(null);
  const [selection, setSelection] = useState<DocumentImportSelection | null>(null);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [mineruError, setMineruError] = useState("");
  const needsOCR = ["pdf", "docx", "pptx"].includes(format);
  useEffect(() => {
    const element = dialog.current!;
    element.showModal();
    return () => element.close();
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    setFailed(false);
    setMineruError("");
    Promise.all([api<RecognitionCatalog>("/settings/recognition", { errorPolicy: "caller", signal: controller.signal }), loadGenerationSettings(controller.signal, "caller"),
      format === "pdf" ? api<{ has_token: boolean }>("/settings/mineru", { errorPolicy: "silent", signal: controller.signal }).catch(error => {
        if (controller.signal.aborted) throw error;
        setMineruError(error instanceof Error ? error.message : String(error));
        return null;
      }) : Promise.resolve({ has_token: false })])
      .then(([recognition, generation, mineru]) => {
        if (controller.signal.aborted) return;
        setData({ recognition, generation, mineru });
        setSelection(current => current || { llm_profile_id: generation.active_id, note_style: generation.style, ocr_option: recognition.settings.ocr_option,
          parser: format === "pdf" && mineru ? mineru.has_token ? "mineru_vlm" : "mineru_agent" : "local" });
      }).catch(error => { if (!controller.signal.aborted) { setFailed(true); onError(error); } });
    return () => controller.abort();
  }, [attempt, onError, format]);
  const model = data?.recognition.models.find(model => model.id === selection?.ocr_option);
  const ocrReady = !needsOCR || !!model?.ready && model.runtime_ready;
  const profileReady = data?.generation.profiles.find(profile => profile.id === selection?.llm_profile_id)?.has_token;
  const parserReady = selection?.parser !== "mineru_vlm" || data?.mineru?.has_token;
  return <dialog ref={dialog} className="settings-dialog analysis-setup-dialog" aria-labelledby="document-import-title"
    onCancel={event => { event.preventDefault(); if (!busy) onClose(); }}>
    <button className="dialog-close" aria-label="关闭文档导入设置" disabled={busy} onClick={onClose}>×</button>
    <h2 id="document-import-title">生成文档笔记</h2>
    <p className="pending-filename">{filename}</p>
    {!data || !selection ? failed ? <div className="reader-state" role="status">读取配置失败。
      <button onClick={() => setAttempt(value => value + 1)}>重新读取配置</button></div> : <p role="status">正在读取生成配置…</p> : <>
      <APIProfileSelect profiles={data.generation.profiles} value={selection.llm_profile_id} disabled={busy}
        onChange={llm_profile_id => setSelection({ ...selection, llm_profile_id })} />
      <NoteStyleSelect value={selection.note_style} disabled={busy} onChange={note_style => setSelection({ ...selection, note_style })} />
      {format === "pdf" && <>
        <label className="note-style-selection">PDF 解析方式<select value={selection.parser} disabled={busy}
          onChange={event => setSelection({ ...selection, parser: event.target.value as DocumentImportSelection["parser"] })}>
          <option value="mineru_agent">MinerU Agent · 轻量解析</option>
          <option value="mineru_vlm" disabled={!data.mineru?.has_token}>MinerU VLM · 精准解析{!data.mineru?.has_token ? "（需配置 Token）" : ""}</option>
          <option value="local">本地提取</option>
        </select></label>
        <p className="muted">{selection.parser === "local" ? "仅在本机提取文字并识别内嵌图片。" : "确认开始后会将 PDF 批次上传至 MinerU，原文件内嵌图片同时在本机 OCR。"}
          {!data.mineru?.has_token && <> <a href="#settings">配置 MinerU Token</a></>}</p>
        {mineruError && <div className="notice" role="alert"><p>{mineruError}</p>
          <p>MinerU 配置读取失败，可选择本地提取，或重新读取设置后选择精准解析。</p>
          <button type="button" disabled={busy} onClick={() => setAttempt(value => value + 1)}>重新读取 MinerU 设置</button></div>}
      </>}
      <label className="note-style-selection">OCR 模型
        <select value={selection.ocr_option} disabled={busy || !needsOCR}
          onChange={event => setSelection({ ...selection, ocr_option: event.target.value })}>
          {data.recognition.models.filter(model => model.kind === "ocr").map(model => <option key={model.id} value={model.id}>
            {model.name}{!model.runtime_ready ? " · 运行组件不可用" : !model.ready ? " · 文件未准备好" : ""}
          </option>)}
        </select>
      </label>
      {needsOCR ? <p className="muted">用于 PDF、Word 或 PPT 内嵌图片，本次选择不改变全局设置。同一文件换用 OCR 模型会保存为独立资料。</p>
        : <p className="muted">此类文档直接提取文字，不需要 OCR。</p>}
      {!ocrReady && <p className="notice" role="status">所选 OCR 模型不可用，请先在设置中检查运行组件和模型文件。</p>}
      {!profileReady && <p className="notice">请先在设置中为此 API 配置填写密钥。</p>}
      <p className="muted">原文件已保存在本机。确认后提取资料并按需 OCR，核对字符数和分段限制，再按所选配置生成笔记。</p>
    </>}
    {busy && <p role="status">{stage}</p>}
    {error && <p className="notice" role="alert">{error}</p>}
    <div className="settings-actions">
      <button disabled={busy} onClick={onClose}>取消</button>
      <button className="primary" disabled={busy || !selection || !profileReady || !ocrReady || !parserReady}
        onClick={async () => { if (selection && await onStart(selection)) onClose(); }}>{busy ? "正在处理…" : "开始生成"}</button>
    </div>
  </dialog>;
}
