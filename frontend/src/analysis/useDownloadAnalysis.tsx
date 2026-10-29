import type { AnalysisSelection } from "../types";
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { TaskReceipt, Video } from "../types";
import {
  AnalysisSetupDialog,
} from "../settings/AnalysisSetupDialog";
import { createAnalysis } from "./requests";

// 将下载成品导入为视频，再把已接受的任务回执交给详情页接管。
export function useDownloadAnalysis({
  blocked,
  refresh,
  onOpen,
  onError,
  onBusyChange,
  navigationKey,
}: {
  blocked: boolean;
  refresh: () => Promise<unknown>;
  onOpen: (video: Video, receipt: TaskReceipt) => void;
  onError: (error: unknown) => void;
  onBusyChange: (busy: boolean) => void;
  navigationKey: string;
}) {
  const [source, setSource] = useState<{ id: string; filename: string } | null>(
    null,
  );
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);
  const lifetime = useRef(new AbortController());
  useEffect(() => {
    lifetime.current = new AbortController();
    setSource(null);
    setBusy(false);
    return () => { lifetime.current.abort(); onBusyChange(false); };
  }, [navigationKey, onBusyChange]);
  // 导入与建任务均同步防重，列表刷新失败也不丢弃已经接受的任务回执。
  async function start(selection: AnalysisSelection) {
    if (!source || blocked || inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    onBusyChange(true);
    onError("");
    const signal = lifetime.current.signal;
    try {
      const video = await api<Video>(`/video-downloads/${source.id}/import`, {
        method: "POST", errorPolicy: "caller",
        signal,
      });
      void refresh().catch(error => { if (!signal.aborted) onError(error); });
      const receipt = await createAnalysis(video.id, selection, signal);
      if (signal.aborted) return;
      setSource(null);
      onOpen(video, receipt);
      void refresh().catch(onError);
    } catch (error) {
      if (!signal.aborted) onError(error);
    } finally {
      inFlight.current = false;
      if (!signal.aborted) { setBusy(false); onBusyChange(false); }
    }
  }
  return {
    busy,
    open: (id: string, filename: string) => setSource({ id, filename }),
    dialog: source && (
      <AnalysisSetupDialog
        filename={source.filename}
        busy={busy || blocked}
        onStart={start}
        onClose={() => setSource(null)}
        onSettings={() => {
          setSource(null);
          location.hash = "settings";
        }}
      />
    ),
  };
}
