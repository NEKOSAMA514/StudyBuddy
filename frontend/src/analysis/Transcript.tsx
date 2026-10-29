import { useEffect, useRef, useState } from "react";
import { transcriptLabel, type Video, type Task, type Evidence } from "../types";
import { evidenceImageUrl, transcriptUrl } from "../api";
import type { ReadState } from "../notes/useNoteResult";
import { clock } from "../util/time";

// 分页展示语音和画面原文，切换来源或页码时隔离旧读取结果。
export function Transcript({
  reading,
  video,
  task,
  evidence,
  availableEvidence,
  jump,
}: {
  reading: ReadState;
  video: Video;
  task: Task | null;
  evidence: Evidence[];
  availableEvidence: Evidence[];
  jump: (seconds: number) => void;
}) {
  const [evidenceLimit, setEvidenceLimit] = useState(30);
  const [evidenceKind, setEvidenceKind] = useState<"speech" | "frame">(
    "speech",
  );
  const evidenceList = useRef<HTMLDivElement>(null);
  const sourceLabel = transcriptLabel(task?.config?.transcript_source);
  const isSubtitle = ["youtube_subtitle", "bilibili_subtitle"].includes(task?.config?.transcript_source?.kind || "");
  useEffect(() => {
    if (evidence.length && !evidence.some((item) => item.kind === "speech"))
      setEvidenceKind("frame");
  }, [evidence]);
  useEffect(() => {
    if (evidenceList.current) evidenceList.current.scrollTop = 0;
  }, [evidenceKind]);
  const filteredEvidence = availableEvidence.filter(
    (item) => item.kind === evidenceKind,
  );
  const shownEvidence = filteredEvidence.slice(0, evidenceLimit);
  return (
    <section className="panel evidence-panel">
      <div className="panel-header">
        <h2>
          原始转录 <span className="count">{evidence.length}</span>
        </h2>
      </div>
      {reading.loading && <p className="reader-state" role="status">正在读取依据…</p>}
      {reading.error && <div className="reader-state" role="status"><span>{reading.error}</span>
        <button className="secondary" onClick={reading.retry}>重新读取依据</button></div>}
      {video && task && (
        <div className="transcript-actions">
          <span>已保存在本机，可复用讲解文本和画面文字</span>
          <a href={transcriptUrl(video.id, task.id, "txt")} download>
            下载 TXT
          </a>
          <a href={transcriptUrl(video.id, task.id, "json")} download>
            下载 JSON
          </a>
        </div>
      )}
      <div className="transcript-tabs" role="tablist" aria-label="转录类型">
        {(
          [
            ["speech", isSubtitle ? sourceLabel : "语音转录"],
            ["frame", "图像识别"],
          ] as const
        ).map(([kind, label]) => (
          <button
            key={kind}
            id={`transcript-tab-${kind}`}
            type="button"
            role="tab"
            aria-selected={evidenceKind === kind}
            aria-controls="transcript-content"
            onClick={() => {
              setEvidenceKind(kind);
              setEvidenceLimit(30);
            }}
          >
            {label}{" "}
            <span>
              {availableEvidence.filter((item) => item.kind === kind).length}
            </span>
          </button>
        ))}
      </div>
      <p className="muted">
        {evidenceKind === "speech"
          ? `${isSubtitle ? sourceLabel : "讲解语音"}的原文，按视频时间排序`
          : "抽样画面截图，按视频时间排序；识别文字保留在转录下载中"}
      </p>
      <div
        className="evidence-list"
        ref={evidenceList}
        role="tabpanel"
        id="transcript-content"
        aria-labelledby={`transcript-tab-${evidenceKind}`}
      >
        {!shownEvidence.length && (
          <p className="evidence-empty">未提取到这类识别结果。</p>
        )}
        {shownEvidence.map((item) => (
          <details
            key={item.id}
            className="evidence-item"
            data-start={item.start_seconds}
          >
            <summary>
              <span className="evidence-type">
                {item.kind === "speech" ? sourceLabel : "画面"}
              </span>
              <span>{clock(item.start_seconds)}</span>
              <span className="evidence-preview">
                {item.kind === "speech" ? item.text : "查看截图"}
              </span>
            </summary>
            <div className="evidence-body">
              <button
                className="time-button"
                onClick={() => jump(item.start_seconds)}
              >
                定位 {clock(item.start_seconds)} ↗
              </button>
              {item.kind === "speech" && <p>{item.text}</p>}
              {item.image_name && (
                <img
                  loading="lazy"
                  src={evidenceImageUrl(item.id)}
                  alt={`${clock(item.start_seconds)} 的原始视频截图`}
                />
              )}
              <small>
                {item.kind === "frame"
                  ? "截图只证明该时刻的画面；OCR 可能有识别错误。"
                  : "语音转录可能误识别专业名词，请回看确认。"}
              </small>
            </div>
          </details>
        ))}
      </div>
      {filteredEvidence.length > evidenceLimit && (
        <button
          className="text-button evidence-more"
          onClick={() => setEvidenceLimit((value) => value + 30)}
        >
          显示更多{evidenceKind === "speech" ? "转录" : "画面"}
        </button>
      )}
    </section>
  );
}
