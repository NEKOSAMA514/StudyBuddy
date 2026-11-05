// 文件和本次配置为主，OCR 就绪状态为次。
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { APIProfileSelect } from "../settings/APIProfileSelect";
import { loadGenerationSettings } from "../settings/generationSettings";
import { NoteStyleSelect } from "../settings/NoteStyleSelect";
import type { RecognitionCatalog, DocumentImportSelection } from "../types";
import { supportsMinerU } from "./mineru";


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
  const cloudAvailable = supportsMinerU(format);
  const cloudPresentation = ["ppt", "pptx"].includes(format) && selection?.parser !== "local";
  const needsOCR = format === "pdf" || format === "docx" || format === "pptx" && !cloudPresentation;
  const parserLabel = format === "pdf" ? "PDF 解析方式" : "PPT 解析方式";
  const localHint = format === "ppt" ? "仅在本机提取幻灯片文字和讲者备注。" : "仅在本机提取文字并识别内嵌图片。";
  const cloudHint = format === "pdf"
    ? "确认开始后会将 PDF 批次上传至 MinerU，原文件内嵌图片同时在本机 OCR。"
    : "确认开始后会将演示文稿上传至 MinerU，由服务解析文字、表格等内容。";
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
      cloudAvailable ? api<{ has_token: boolean }>("/settings/mineru", { errorPolicy: "silent", signal: controller.signal }).catch(error => {
        if (controller.signal.aborted) throw error;
        setMineruError(error instanceof Error ? error.message : String(error));
        return null;
      }) : Promise.resolve({ has_token: false })])
      .then(([recognition, generation, mineru]) => {
        if (controller.signal.aborted) return;
        setData({ recognition, generation, mineru });
        const cloudParser = mineru?.has_token ? "mineru_vlm" : "mineru_agent";
        setSelection(current => current || { llm_profile_id: generation.active_id, note_style: generation.style, ocr_option: recognition.settings.ocr_option,
          parser: cloudAvailable && mineru ? cloudParser : "local" });
      }).catch(error => { if (!controller.signal.aborted) { setFailed(true); onError(error); } });
    return () => controller.abort();
  }, [attempt, onError, format]);
  const model = data?.recognition.models.find(model => model.id === selection?.ocr_option);
  const ocrReady = !needsOCR || !!model?.ready && model.runtime_ready;
  const profileReady = !!data?.generation.profiles.find(profile => profile.id === selection?.llm_profile_id)?.has_token;
  const parserReady = selection?.parser !== "mineru_vlm" || data?.mineru?.has_token;
  return <dialog ref={dialog} className="settings-dialog analysis-setup-dialog" aria-labelledby="document-import-title"
    onCancel={event => { event.preventDefault(); if (!busy) onClose(); }}>
    <button className="dialog-close" aria-label="关闭文档导入设置" disabled={busy} onClick={onClose}>×</button>
    <h2 id="document-import-title">{data && !profileReady ? "提取文档" : "生成文档笔记"}</h2>
    <p className="pending-filename">{filename}</p>
    {!data || !selection ? failed ? <div className="reader-state" role="status">读取配置失败。
      <button onClick={() => setAttempt(value => value + 1)}>重新读取配置</button></div> : <p role="status">正在读取生成配置…</p> : <>
      <APIProfileSelect profiles={data.generation.profiles} value={selection.llm_profile_id} disabled={busy}
        onChange={llm_profile_id => setSelection({ ...selection, llm_profile_id })} />
      {profileReady && <NoteStyleSelect value={selection.note_style} disabled={busy}
        onChange={note_style => setSelection({ ...selection, note_style })} />}
      {cloudAvailable && <>
        <label className="note-style-selection">{parserLabel}<select aria-label={parserLabel} value={selection.parser} disabled={busy}
          onChange={event => setSelection({ ...selection, parser: event.target.value as DocumentImportSelection["parser"] })}>
          <option value="local">本地提取</option>
          <option value="mineru_agent">MinerU Agent · 轻量解析</option>
          <option value="mineru_vlm" disabled={!data.mineru?.has_token}>MinerU VLM · 精准解析{!data.mineru?.has_token ? "（需配置 MinerU Token）" : ""}</option>
        </select></label>
        <p className="muted">{selection.parser === "local" ? localHint : cloudHint}
          {selection.parser !== "local" && !data.mineru?.has_token && <> <a href="#settings">配置 MinerU Token 以启用精准解析</a></>}</p>
        {mineruError && <div className="notice" role="alert"><p>{mineruError}</p>
          <p>MinerU 配置读取失败，可选择本地提取，或重新读取设置后选择精准解析。</p>
          <button type="button" disabled={busy} onClick={() => setAttempt(value => value + 1)}>重新读取 MinerU 设置</button></div>}
      </>}
      {!cloudPresentation && <>
        <label className="note-style-selection">OCR 模型
          <select aria-label="OCR 模型" value={selection.ocr_option} disabled={busy || !needsOCR}
            onChange={event => setSelection({ ...selection, ocr_option: event.target.value })}>
            {data.recognition.models.filter(model => model.kind === "ocr").map(model => <option key={model.id} value={model.id}>
              {model.name}{!model.runtime_ready ? " · 运行组件不可用" : !model.ready ? " · 文件未准备好" : ""}
            </option>)}
          </select>
        </label>
        {needsOCR ? <p className="muted">用于 PDF、Word 或 PPT 内嵌图片，本次选择不改变全局设置。同一文件换用 OCR 模型会保存为独立资料。</p>
          : <p className="muted">此类文档直接提取文字，不需要 OCR。</p>}
      </>}
      {!ocrReady && <p className="notice" role="status">所选 OCR 模型不可用，请先在设置中检查运行组件和模型文件。</p>}
      <p className="muted">{profileReady
        ? `原文件已保存在本机。确认后提取资料${needsOCR ? "并按需 OCR" : ""}，再按所选配置生成笔记。`
        : "本次只提取资料，完成后可查看提取内容。配置模型 API 密钥后，可在资料页生成笔记。"}</p>
    </>}
    {busy && <p role="status">{stage}</p>}
    {error && <p className="notice" role="alert">{error}</p>}
    <div className="settings-actions">
      <button disabled={busy} onClick={onClose}>取消</button>
      <button className="primary" disabled={busy || !selection || !ocrReady || !parserReady}
        onClick={async () => { if (selection && await onStart({ ...selection, generate_notes: profileReady })) onClose(); }}>
        {busy ? "正在处理…" : profileReady ? "开始生成" : "开始提取"}</button>
    </div>
  </dialog>;
}
