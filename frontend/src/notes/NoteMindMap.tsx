// 原位显示导图和进度。配置弹窗以生成动作为主，大图以原图为主。
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { api, apiUrl, errorText, type ErrorReference } from "../api";
import { APIProfileSelect } from "../settings/APIProfileSelect";
import { loadAPIProfiles } from "../settings/generationSettings";
import { useScopedRead } from "../shared/useScopedRead";

type MapStatus = { status: "idle" | "generating" | "failed"; image_revision: string | null; error: ErrorReference | null };
type DialogMode = "create" | "replace" | "preview";
const inlineReadError = () => {};

// 每个阅读面板已有版本 key。旧请求在面板卸载时取消，服务端生成继续运行。
export function useNoteMindMap(kind: "video" | "document", taskId?: string) {
  const path = taskId ? `/note-mind-maps/${kind}/${encodeURIComponent(taskId)}` : "";
  const reading = useScopedRead<MapStatus>({ identity: path, path, enabled: !!path,
    errorMessage: "思维导图状态读取失败。", onError: inlineReadError });
  const [receipt, setReceipt] = useState<MapStatus | null>(null);
  const [dialog, setDialog] = useState<DialogMode | null>(null);
  const state = reading.confirmed ? reading.data : receipt || reading.data;
  const running = state?.status === "generating";
  const image = state?.image_revision ? apiUrl(`${path}/image?v=${state.image_revision}`) : null;
  const retry = reading.retry;
  useEffect(() => {
    if (reading.confirmed) setReceipt(null);
  }, [reading.confirmed]);
  useEffect(() => {
    if (!running || reading.loading) return;
    const timer = window.setTimeout(retry, reading.error ? 4000 : 1200);
    return () => window.clearTimeout(timer);
  }, [running, reading.loading, reading.error, retry]);

  const actions = path ? [{
    label: image ? "查看思维导图" : running ? "正在生成思维导图…" : reading.error ? "重新读取思维导图" : "生成思维导图",
    disabled: !image && (running || (!state && reading.loading)),
    onSelect: () => {
      if (image) setDialog("preview");
      else if (reading.error) retry();
      else setDialog("create");
    },
  }] : [];
  if (image) actions.push({ label: "重新生成思维导图", disabled: running, onSelect: () => setDialog("replace") });

  async function generate(profile: string, replace: boolean, signal: AbortSignal) {
    try {
      const value = await api<MapStatus>(path, { method: "POST", signal, errorPolicy: "caller",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ llm_profile_id: profile, replace_existing: replace }) });
      if (!signal.aborted) {
        setReceipt(value);
        setDialog(null);
      }
    } finally {
      if (!signal.aborted) retry();
    }
  }

  return { actions, dialog, setDialog, running, image, generate, retry, readError: reading.error,
    generationError: state?.error };
}

function MindMapDialog({ title, children, fullscreen, busy, close }: {
  title: string; children: ReactNode; fullscreen?: boolean; busy?: boolean; close: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const heading = useId();
  useEffect(() => {
    const element = dialog.current!;
    element.showModal();
    return () => element.close();
  }, []);
  return <dialog ref={dialog} aria-label={fullscreen ? title : undefined} aria-labelledby={fullscreen ? undefined : heading}
    className={fullscreen ? "mind-map-fullscreen" : "settings-dialog analysis-setup-dialog mind-map-setup"}
    onCancel={event => { event.preventDefault(); if (!busy) close(); }}>
    <button type="button" className="dialog-close" disabled={busy} aria-label="关闭思维导图" onClick={close}>×</button>
    {!fullscreen && <h2 id={heading}>{title}</h2>}
    {children}
  </dialog>;
}

function MindMapSetup({ replace, generate, close }: {
  replace: boolean; generate: (profile: string, replace: boolean, signal: AbortSignal) => Promise<void>;
  close: () => void;
}) {
  const lifetime = useRef<AbortController | null>(null);
  const [profiles, setProfiles] = useState<Awaited<ReturnType<typeof loadAPIProfiles>> | null>(null);
  const [selected, setSelected] = useState("");
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [submitError, setSubmitError] = useState("");
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    loadAPIProfiles(controller.signal, "caller").then(value => {
      if (controller.signal.aborted) return;
      setProfiles(value);
      setSelected(value.active_id);
    }).catch(error => {
      if (!controller.signal.aborted) setLoadError(error.message || "无法读取 API 配置。");
    });
    return () => controller.abort();
  }, [attempt]);

  const hasToken = profiles?.profiles.find(profile => profile.id === selected)?.has_token;
  async function submit() {
    if (!hasToken || busy || !lifetime.current) return;
    const { signal } = lifetime.current;
    setBusy(true);
    setSubmitError("");
    try {
      await generate(selected, replace, signal);
    } catch (error) {
      if (!signal.aborted) setSubmitError(error instanceof Error ? error.message : "提交失败，请重试。");
    } finally {
      if (!signal.aborted) setBusy(false);
    }
  }
  return <MindMapDialog title={replace ? "重新生成思维导图" : "生成思维导图"} busy={busy} close={close}>
    <p className="muted">用所选模型，将当前笔记版本整理成一张 PNG 思维导图。</p>
    <form onSubmit={event => { event.preventDefault(); void submit(); }}>
      {profiles ? <>
        <APIProfileSelect profiles={profiles.profiles} value={selected} disabled={busy}
          onChange={id => { setSelected(id); setSubmitError(""); }} />
        {!hasToken && <p className="notice">请先在模型设置中为所选 API 配置填写密钥。</p>}
      </> : loadError ? <div className="reader-state" role="status">
        <span>{loadError}</span><button className="secondary" type="button"
          onClick={() => { setLoadError(""); setAttempt(value => value + 1); }}>重新读取配置</button>
      </div> : <p role="status">正在读取 API 配置…</p>}
      {submitError && <p className="notice" role="alert">{submitError}</p>}
      <div className="settings-actions">
        <button type="button" disabled={busy} onClick={close}>取消</button>
        <button type="submit" className="primary" disabled={!hasToken || busy}>{busy ? "正在提交…" : "开始生成"}</button>
      </div>
    </form>
  </MindMapDialog>;
}

export function NoteMindMap({ map, title }: { map: ReturnType<typeof useNoteMindMap>; title: string }) {
  const { dialog, setDialog, image, running, generationError, readError } = map;
  const close = () => setDialog(null);
  return <>
    {(image || running || generationError || readError) && <section className="note-mind-map" aria-label="思维导图">
      <span className="eyebrow">思维导图</span>
      {image && <button type="button" className="mind-map-thumbnail" aria-label="放大思维导图" onClick={() => setDialog("preview")}>
        <img src={image} alt={`${title}的思维导图`} />
      </button>}
      {running && <p className="muted" role="status">正在生成思维导图…</p>}
      {generationError && <div className="reader-state" role="status"><span>{errorText(generationError, "思维导图生成失败。")}</span>
        <button type="button" className="secondary" onClick={() => setDialog(image ? "replace" : "create")}>重试生成</button>
      </div>}
      {readError && <div className="reader-state" role="status"><span>{readError}</span>
        <button type="button" className="secondary" onClick={map.retry}>重新读取</button>
      </div>}
    </section>}
    {dialog === "preview" && image && <MindMapDialog title="思维导图" fullscreen close={close}>
      <img src={image} alt={`${title}的思维导图`} />
    </MindMapDialog>}
    {(dialog === "create" || dialog === "replace") &&
      <MindMapSetup replace={dialog === "replace"} generate={map.generate} close={close} />}
  </>;
}
