import { notifyError } from "../notifications";
import { readBilibiliSession, publishBilibiliSession, subscribeBilibiliSession } from "./bilibiliSession";
import { useEffect, useRef, useState } from "react";
import { apiUrl, errorText, isAbortError, ReportedError, requestJSON } from "../api";
import type { BilibiliSession } from "../types";

// 识别 BV 号或单个 B 站分享链接，决定是否先展示登录衔接。
function isBilibili(url: string) {
  if (/^BV[0-9A-Za-z]{10}$/.test(url.trim())) return true;
  const links = url.match(/https?:\/\/[^\s<>"'【】（）()，。！？、；]+/gi);
  if (links?.length !== 1) return false;
  try { return ["bilibili.com", "www.bilibili.com", "m.bilibili.com", "b23.tv", "www.b23.tv"].includes(new URL(links[0]).hostname.toLowerCase()); }
  catch { return false; }
}

export type QualityOption = { height: number; ext: string | null; filesize: number | null; approximate: boolean };
type VideoPart = { page: number; cid: number; title: string; duration: number; url: string };
type Metadata = { url: string; title: string; thumbnail: string | null; duration: number | null; qualities: number[]; default_quality: number | null; quality_options?: QualityOption[]; parts?: VideoPart[]; selected_part?: number };
type DownloadTask = {
  id: string; url: string; quality: number; metadata: Metadata | null; path: string | null;
  status: "running" | "succeeded" | "failed" | "interrupted"; stage: string; progress: number | null;
  error: { message: string; error_id?: string; log_file?: string } | null;
};
const storageKey = "studybuddy.videoDownloadTaskId";
// 在标签页中保存下载任务编号，存储受限时继续依赖当前内存状态。
function storedTask(value?: string | null): string | null {
  try {
    if (value === undefined) return sessionStorage.getItem(storageKey);
    if (value === null) sessionStorage.removeItem(storageKey);
    else sessionStorage.setItem(storageKey, value);
  } catch { /* 当前页继续使用内存状态。 */ }
  return null;
}

// 管理链接解析、分 P、登录衔接和下载恢复，展示组件只转交操作。
export function useVideoDownload() {
  const [url, setUrl] = useState("");
  const [metadata, setMetadata] = useState<Metadata | null>(null);
  const [quality, setQuality] = useState<number | "">("");
  const [selectedPart, setSelectedPart] = useState(1);
  const [metadataReady, setMetadataReady] = useState(false);
  const [bilibiliSession, setBilibiliSession] = useState<BilibiliSession | null>(null);
  const [loginIntent, setLoginIntent] = useState<{ url?: string; immediate: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const [openingFolder, setOpeningFolder] = useState(false);
  const [task, setTask] = useState<DownloadTask | null>(null);
  const [pollingError, setPollingError] = useState(false);
  const [pollVersion, setPollVersion] = useState(0);
  const [identifier, setIdentifier] = useState(() => storedTask());
  const notified = useRef(new Set<string>());
  const freshTask = useRef<string | null>(null);
  const parseController = useRef<AbortController | null>(null);
  const sessionController = useRef<AbortController | null>(null);
  const bilibiliLink = isBilibili(url);
  const running = task?.status === "running";
  const localPath = task?.status === "succeeded" ? task.path : null;
  const pendingPart = !metadataReady ? metadata?.parts?.find(part => part.page === selectedPart) : undefined;
  const displayedDuration = pendingPart?.duration ?? metadata?.duration;

  useEffect(() => () => { parseController.current?.abort(); }, []);

  useEffect(() => {
    // 已确认的会话变更先取消旧查询，避免迟到结果恢复已退出的状态。
    return subscribeBilibiliSession(session => {
      const controller = sessionController.current;
      controller?.abort();
      if (controller && parseController.current === controller) setBusy(false);
      setBilibiliSession(session);
    });
  }, []);

  useEffect(() => {
    if (!bilibiliLink) return;
    const controller = new AbortController();
    sessionController.current = controller;
    void readBilibiliSession(controller.signal)
      .then(session => { if (!controller.signal.aborted) setBilibiliSession(session); })
      .catch(() => { /* requestJSON 已处理错误；查询失败不改变登录状态。 */ });
    return () => controller.abort();
  }, [bilibiliLink]);

  useEffect(() => {
    if (!identifier) return;
    const controller = new AbortController();
    let timer: number | undefined;
    async function poll() {
      try {
        const next = await requestJSON<DownloadTask>(apiUrl(`/video-downloads/${identifier}`), { signal: controller.signal, cache: "no-store" });
        if (controller.signal.aborted) return;
        setPollingError(false);
        setTask(next);
        setUrl(next.url);
        if (next.metadata) { setMetadata(next.metadata); setMetadataReady(true); setQuality(next.quality); setSelectedPart(next.metadata.selected_part ?? 1); }
        if (next.status === "running") {
          freshTask.current = next.id;
          timer = window.setTimeout(() => void poll(), 1000);
        }
        else if (next.error && freshTask.current === next.id && !notified.current.has(next.id)) {
          notified.current.add(next.id);
          notifyError(errorText(next.error));
        }
      } catch (error) {
        if (!isAbortError(error)) setPollingError(true);
      }
    }
    void poll();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [identifier, pollVersion]);

  // 更换链接或分 P 时取消旧解析，重新确认画质后才允许下载。
  async function loadMetadata(nextUrl: string) {
    parseController.current?.abort();
    const controller = new AbortController();
    parseController.current = controller;
    setBusy(true);
    setMetadataReady(false);
    setQuality("");
    try {
      const result = await requestJSON<Metadata>(apiUrl("/video-downloads/parse"), {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url: nextUrl }), signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      setMetadata(result);
      setMetadataReady(true);
      setUrl(result.url);
      setSelectedPart(result.selected_part ?? 1);
      setQuality(result.default_quality ?? "");
    } catch { /* requestJSON 已通过统一通知展示错误，取消请求不提示。 */ }
    finally { if (!controller.signal.aborted) setBusy(false); }
  }

  // 解析新链接前清理旧下载引用，B 站未登录时先等待用户选择。
  async function parse() {
    setIdentifier(null);
    storedTask(null);
    setTask(null);
    setPollingError(false);
    setMetadata(null);
    if (bilibiliLink) {
      sessionController.current?.abort();
      parseController.current?.abort();
      const controller = new AbortController();
      parseController.current = controller;
      sessionController.current = controller;
      setBusy(true);
      try {
        const session = await readBilibiliSession(controller.signal);
        if (controller.signal.aborted) return;
        setBilibiliSession(session);
        if (session.state !== "logged_in") {
          setLoginIntent({ url, immediate: false });
          setBusy(false);
          return;
        }
      } catch {
        if (controller.signal.aborted) return;
        setLoginIntent({ url, immediate: false });
        setBusy(false);
        return;
      }
    }
    await loadMetadata(url);
  }

  function continueParsing() {
    const nextUrl = loginIntent?.url;
    setLoginIntent(null);
    if (nextUrl) void loadMetadata(nextUrl);
  }

  function selectPart(page: number) {
    const part = metadata?.parts?.find(item => item.page === page);
    if (!part) return;
    setSelectedPart(page);
    setUrl(part.url);
    setTask(null);
    setIdentifier(null);
    storedTask(null);
    void loadMetadata(part.url);
  }

  // 只按已确认的链接与画质创建下载，并保存任务编号供刷新后读取。
  async function start() {
    if (!metadata || !metadataReady || quality === "" || busy) return;
    setBusy(true);
    try {
      const result = await requestJSON<DownloadTask>(apiUrl("/video-downloads"), {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url: metadata.url, quality }),
      });
      freshTask.current = result.id;
      storedTask(result.id);
      setTask(result);
      setIdentifier(result.id);
    } catch { /* 请求失败只显示统一通知。 */ }
    finally { setBusy(false); }
  }

  async function openFolder() {
    if (!task || task.status !== "succeeded") return;
    setOpeningFolder(true);
    try {
      await requestJSON(apiUrl(`/video-downloads/${task.id}/reveal`), {
        method: "POST", signal: AbortSignal.timeout(5000),
      });
    } catch (error) {
      if (!(error instanceof ReportedError)) throw error;
    } finally { setOpeningFolder(false); }
  }

  return {
    input: { url, metadata, quality, selectedPart, metadataReady, pendingPart, displayedDuration, busy, bilibiliLink,
      parse, selectPart, selectQuality: setQuality,
      changeUrl: (value: string) => { setUrl(value); setMetadata(null); setQuality(""); setTask(null); } },
    download: { task, running, localPath, openingFolder, pollingError, start, openFolder,
      retry: () => setPollVersion(value => value + 1) },
    login: { session: bilibiliSession, intent: loginIntent, continueParsing,
      open: () => setLoginIntent({ immediate: true }), close: () => setLoginIntent(null),
      complete: (session: BilibiliSession) => { publishBilibiliSession(session); continueParsing(); } },
  };
}
