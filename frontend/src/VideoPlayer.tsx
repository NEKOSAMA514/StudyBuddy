import { useEffect, useRef, useState, type RefObject } from "react";
import { apiUrl, ReportedError, errorText, isAbortError, requestJSON } from "./api";

type Playback = {
  status: "unprepared" | "queued" | "preparing" | "ready" | "failed";
  original_url: string;
  mime_type: string;
  direct: boolean;
  url: string | null;
  error: { message: string; error_id?: string; log_file?: string } | null;
};

// 先尝试原件播放，按后端状态准备兼容副本，并在元数据就绪后定位时间。
export function VideoPlayer({ id, playerRef, seek, onError }: {
  id: string;
  playerRef: RefObject<HTMLVideoElement | null>;
  seek: number | null;
  onError: (error: unknown) => void;
}) {
  const [state, setState] = useState<Playback | null>(null);
  const [source, setSource] = useState("");
  const [requestFailed, setRequestFailed] = useState(false);
  const request = useRef<((retry?: boolean) => void) | null>(null);
  const position = useRef(seek || 0);
  const resume = useRef(false);
  const latestError = useRef(onError);
  latestError.current = onError;
  useEffect(() => { if (seek !== null) position.current = seek; }, [seek]);

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let preparing = false;
    let knownFailure: string | null = null;
    let firstRead = true;
    let forceCopy = false;
    const endpoint = apiUrl(`/videos/${id}/playback`);
    async function read(method = "GET") {
      if (preparing) return;
      preparing = true;
      try {
        const next = await requestJSON<Playback>(endpoint, { errorPolicy: "caller", method, signal: controller.signal });
        if (controller.signal.aborted) return;
        setRequestFailed(false);
        setState(next);
        const originalPlayable = !forceCopy && next.direct && document.createElement("video").canPlayType(next.mime_type);
        if (originalPlayable) setSource(next.original_url);
        else if (next.status === "ready") setSource(next.url!);
        else if (next.status === "unprepared") {
          firstRead = false;
          preparing = false;
          void read("POST");
          return;
        } else if (next.status === "queued" || next.status === "preparing") {
          timer = setTimeout(() => void read(), 750);
        } else if (next.status === "failed") {
          const failure = next.error?.error_id || next.error?.message || "failed";
          if (!firstRead && failure !== knownFailure && next.error) {
            latestError.current(new ReportedError(errorText(next.error)));
          }
          knownFailure = failure;
        }
        firstRead = false;
      } catch (error) {
        if (!isAbortError(error)) {
          setRequestFailed(true);
          latestError.current(error);
        }
      } finally { preparing = false; }
    }
    request.current = (retry = false) => {
      clearTimeout(timer);
      if (!retry) playerRef.current?.load();
      if (retry) {
        forceCopy = true;
        position.current = playerRef.current?.currentTime || position.current;
        resume.current = !!playerRef.current && !playerRef.current.paused;
        setSource("");
      }
      void read(retry ? "POST" : "GET");
    };
    void read();
    return () => { controller.abort(); clearTimeout(timer); request.current = null; };
  }, [id, playerRef]);

  return <>
    <video ref={playerRef} src={source || undefined} controls preload="metadata"
      onTimeUpdate={event => { if (source && event.currentTarget.readyState >= 1) position.current = event.currentTarget.currentTime; }}
      onLoadedMetadata={event => {
        event.currentTarget.currentTime = position.current;
        if (resume.current) void event.currentTarget.play().catch(() => {});
      }}
      onError={event => {
        if (!source) return;
        const code = event.currentTarget.error?.code;
        if (source === state?.original_url && (code === 3 || code === 4)) request.current?.(true);
        else {
          setRequestFailed(true);
          latestError.current(new ReportedError("浏览器无法播放这个文件，请重试。"));
        }
      }} />
    {(!state || state.status === "queued" || state.status === "preparing") && !source && !requestFailed &&
      <p className="playback-status" role="status">正在准备播放视频</p>}
    {(requestFailed || !source && state?.status === "failed") && <div className="playback-status">
      <span>播放视频暂不可用，可继续开始处理。</span>
      <button className="secondary" onClick={() => request.current?.(!requestFailed || state?.status === "ready")}>重试播放</button>
    </div>}
  </>;
}
