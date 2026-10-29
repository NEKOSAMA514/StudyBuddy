import { notifyError } from "../notifications";
import { APIProfileSelect } from "./APIProfileSelect";
import { loadGenerationSettings, type APIProfileSummary } from "./generationSettings";
import { useEffect, useRef, useState } from "react";
import type { RecognitionCatalog, AnalysisSelection } from "../types";
import type { VideoAnalysisPreview } from "../types";
import { NoteStyleSelect } from "./NoteStyleSelect";
import { apiUrl, ReportedError, isAbortError, reportClientError, requestJSON } from "../api";


// 创建视频任务前确认本次识别与 API 配置，预检结果绑定当前选择。
export function AnalysisSetupDialog({
  videoId,
  filename,
  busy,
  onStart,
  onClose,
  onSettings,
}: {
  videoId?: string;
  filename: string;
  busy: boolean;
  onStart: (selection: AnalysisSelection) => Promise<void>;
  onClose: () => void;
  onSettings: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [catalog, setCatalog] = useState<RecognitionCatalog | null>(null);
  const [selection, setSelection] = useState<AnalysisSelection | null>(null);
  const [profiles, setProfiles] = useState<APIProfileSummary[]>([]);
  const [failed, setFailed] = useState(false);
  const [preview, setPreview] = useState<{ key: string; value: VideoAnalysisPreview | null } | null>(null);
  const previewKey = JSON.stringify([videoId, selection]);
  const ready = selection && catalog && [selection.asr_option, selection.ocr_option].every(id => {
    const model = catalog.models.find(item => item.id === id);
    return model?.ready && model.runtime_ready && catalog.active_download !== id;
  });

  useEffect(() => {
    if (!videoId || !selection || !ready) return;
    const controller = new AbortController();
    const params = new URLSearchParams(selection);
    requestJSON<VideoAnalysisPreview>(apiUrl(`/videos/${videoId}/analysis-preview?${params}`), { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setPreview({ key: previewKey, value }); })
      .catch(error => {
        if (!controller.signal.aborted && !isAbortError(error)) setPreview({ key: previewKey, value: null });
      });
    return () => controller.abort();
  }, [previewKey, !!ready]);

  useEffect(() => {
    const element = dialog.current!;
    element.showModal();
    const controller = new AbortController();
    Promise.all([
      requestJSON<RecognitionCatalog>(apiUrl("/settings/recognition"), { signal: controller.signal }),
      loadGenerationSettings(controller.signal),
    ])
      .then(([recognition, llm]) => {
        if (controller.signal.aborted) return;
        setCatalog(recognition);
        setSelection({ asr_option: recognition.settings.asr_option, ocr_option: recognition.settings.ocr_option,
          llm_profile_id: llm.active_id, note_style: llm.style });
        setProfiles(llm.profiles);
      })
      .catch((error) => {
        if (controller.signal.aborted || isAbortError(error)) return;
        setFailed(true);
        if (!(error instanceof ReportedError)) throw error;
      });
    return () => {
      controller.abort();
      element.close();
    };
  }, []);

  async function start() {
    if (!selection) return;
    try {
      await onStart(selection);
    } catch (error) {
      if (isAbortError(error)) return;
      if (!(error instanceof ReportedError)) {
        notifyError(await reportClientError(error, (error as Error).message));
      }
    }
  }

  const unsupportedLanguage =
    selection &&
    selection.asr_option === "paraformer" && catalog?.settings.language === "en";
  const selectedProfile = profiles.find(profile => profile.id === selection?.llm_profile_id);

  return (
    <dialog
      ref={dialog}
      className="settings-dialog analysis-setup-dialog"
      aria-labelledby="analysis-setup-title"
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) onClose();
      }}
    >
      <button
        className="dialog-close"
        aria-label="关闭本次模型选择"
        disabled={busy}
        onClick={onClose}
      >
        <svg viewBox="0 0 20 20" aria-hidden="true">
          <path d="m5 5 10 10M15 5 5 15" />
        </svg>
      </button>
      <div className="model-settings">
        <div className="heading">
          <div>
            <span className="eyebrow">本次分析</span>
            <h1 id="analysis-setup-title">选择处理模型</h1>
            <p className="pending-filename">{filename}</p>
          </div>
        </div>
        <div className="video-analysis-preview" role="status">
          {!videoId || selection && !ready ? <p>识别完成后可查看字符数与预计段数。</p>
            : !selection || preview?.key !== previewKey ? <p>正在检查已有识别材料…</p>
            : !preview.value ? <p>暂无法读取材料规模，可继续开始处理。</p>
            : preview.value.character_count === null ? <p>{preview.value.reason}</p>
            : <><p>{preview.value.character_count.toLocaleString()} 字符 · {preview.value.estimated_segments === null
              ? "暂无法估算片段" : `预计 ${preview.value.estimated_segments} 段`}</p>
              {preview.value.reason && <p className="muted">{preview.value.reason}</p>}
              <p className="muted">按已有识别文字估算，实际以本次识别结果为准；片段数不是费用或耗时估价。</p></>}
        </div>
        <p className="muted">
          仅用于这次任务，不修改模型设置中的默认选择。确认后开始处理，取消后文件仍会保留。
        </p>
        <p className="muted">YouTube 和 B 站视频优先读取平台字幕，B 站可扫码登录获取字幕；没有可用字幕时使用语音识别，语言沿用模型设置。</p>
        {selection && catalog ? (
          <form
            className="settings-card"
            onSubmit={(event) => {
              event.preventDefault();
              void start();
            }}
          >
            {(["asr", "ocr"] as const).map((kind) => {
              const field = kind === "asr" ? "asr_option" : "ocr_option";
              const model = catalog.models.find(
                (item) => item.id === selection[field],
              );
              return (
                <label key={kind}>
                  {kind === "asr" ? "语音识别模型" : "OCR 模型"}
                  <select
                    aria-label={kind === "asr" ? "语音识别模型" : "OCR 模型"}
                    value={selection[field]}
                    disabled={busy}
                    onChange={(event) =>
                      setSelection({
                        ...selection,
                        [field]: event.target.value,
                      })
                    }
                  >
                    {catalog.models
                      .filter((item) => item.kind === kind)
                      .map((item) => (
                        <option key={item.id} value={item.id}>
                          {item.name}
                          {!item.ready
                            ? " · 未下载"
                            : !item.runtime_ready
                              ? " · 引擎未安装"
                              : ""}
                        </option>
                      ))}
                  </select>
                  <small>{kind === "asr" ? "无可用平台字幕时使用。" : ""}{model?.description}</small>
                </label>
              );
            })}
            <APIProfileSelect profiles={profiles} value={selection.llm_profile_id} disabled={busy}
              onChange={llm_profile_id => setSelection({ ...selection, llm_profile_id })} />
            <small>使用该配置保存的模型与调用参数；笔记要求沿用全局设置。</small>
            <NoteStyleSelect value={selection.note_style} disabled={busy}
              onChange={note_style => setSelection({ ...selection, note_style })} />
            {(!ready || unsupportedLanguage || !selectedProfile?.has_token) && (
              <p className="notice">
                {!ready
                  ? "所选识别模型尚未就绪，请先在模型设置中下载或准备文件。"
                  : unsupportedLanguage
                    ? "所选语音模型不支持此语言，请更换模型或语言。"
                    : "所选 API 配置不可用或尚未保存密钥，请先到模型设置填写。"}
              </p>
            )}
            <div className="settings-actions">
              <button type="button" disabled={busy} onClick={onClose}>
                取消
              </button>
              <button type="button" disabled={busy} onClick={onSettings}>
                模型设置
              </button>
              <button
                className="primary"
                type="submit"
                disabled={
                  busy || !ready || !!unsupportedLanguage || !selectedProfile?.has_token
                }
              >
                {busy ? "正在提交…" : "开始处理"}
              </button>
            </div>
          </form>
        ) : failed ? null : (
          <p role="status">正在读取模型…</p>
        )}
      </div>
    </dialog>
  );
}
