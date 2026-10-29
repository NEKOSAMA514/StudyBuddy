import { transcriptLabel, type Evidence, type TranscriptSource } from "../types";
import { clock } from "../util/time";
import { evidenceImageUrl } from "../api";

// 展示按时间排列的真实依据，时间入口只通知播放器定位。
export function EvidenceTable({
  items,
  onJump,
  transcriptSource,
}: {
  items: Evidence[];
  onJump: (seconds: number) => void;
  transcriptSource?: TranscriptSource;
}) {
  return (
    <div className="citation-table">
      <table>
        <thead>
          <tr>
            <th scope="col">时间 / 来源</th>
            <th scope="col">原始依据</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.id}>
              <td>
                <button
                  className="time-button"
                  onClick={() => onJump(item.start_seconds)}
                >
                  {clock(item.start_seconds)} ↗
                </button>
                <span className="evidence-type">
                  {item.kind === "speech" ? transcriptLabel(transcriptSource) : "画面"}
                </span>
              </td>
              <td>
                <button
                  className="citation-content"
                  onClick={() => onJump(item.start_seconds)}
                  aria-label={`回看 ${clock(item.start_seconds)} 的${item.kind === "speech" ? transcriptLabel(transcriptSource) : "画面"}`}
                >
                  {item.kind === "speech" ? (
                    <span>{item.text}</span>
                  ) : (
                    <img
                      loading="lazy"
                      src={evidenceImageUrl(item.id)}
                      alt={`${clock(item.start_seconds)} 的原始视频截图`}
                    />
                  )}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {!items.length && <p className="evidence-empty">对应依据尚未加载。</p>}
    </div>
  );
}
