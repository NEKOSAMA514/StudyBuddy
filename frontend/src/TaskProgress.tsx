import type { Task } from "./types";
import { clock } from "./util/time";

type Progress = Pick<Task["progress"], "steps" | "percent" | "completed_steps" | "current_step">
  & Partial<Pick<Task["progress"], "segment" | "processed" | "total">>;

// 按后端提供的步骤和计数呈现进度，避免页面自行推算实际执行状态。
export function TaskProgress({
  progress,
  status = "queued",
  label = "视频分析",
  detail,
}: {
  progress?: Progress;
  status?: string;
  label?: string;
  detail?: string;
}) {
  if (!progress) return <p className="muted">等待开始处理</p>;
  const p = progress;
  const steps = p.steps;
  const phaseNames = { planning: "规划章节", summarizing: "生成章节正文", issues: "校验引用",
    completed: "已保存", overview: "生成整体概览" };
  const failed = ["failed", "interrupted"].includes(status);
  return (
    <div className={`workflow-progress ${failed ? "has-failed" : ""}`}>
      <div className="progress-caption">
        <span>步骤完成度</span>
        <strong>{p.percent}%</strong>
      </div>
      <div
        className="progress-track"
        role="progressbar"
        aria-label={`${label}步骤完成度`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={p.percent}
        aria-valuetext={`${p.percent}%，已完成 ${p.completed_steps} 个步骤，共 ${steps.length} 步`}
      >
        <div style={{ width: `${p.percent}%` }} />
      </div>
      <ol className="workflow-steps" aria-label={`${label}的${steps.length}个步骤`}>
        {steps.map(({ key, title, description }, i) => {
          const done = i < p.completed_steps;
          const current = i + 1 === p.current_step && status !== "succeeded";
          return (
            <li
              key={key}
              data-stage={key}
              data-current={current ? "true" : undefined}
              data-done={done ? "true" : undefined}
              className={done ? "done" : current ? "current" : ""}
              aria-current={current ? "step" : undefined}
            >
              <span className="step-number">{done ? "✓" : i + 1}</span>
              <div>
                <strong>
                  {i + 1}. {title}
                </strong>
                <small>{description}</small>
                {current && detail && <small>{detail}</small>}
                {current && p.segment && <small>
                  第 {p.segment.current}/{p.segment.total} 段 · {clock(p.segment.start_seconds)}–{clock(p.segment.end_seconds)}
                  {" · "}{phaseNames[p.segment.phase]}
                </small>}
              </div>
              <span className="step-state">
                {done
                  ? "已完成"
                  : current
                    ? failed || status === "paused"
                      ? "已停止"
                      : status === "stopping"
                        ? "正在停止"
                        : status === "queued"
                          ? "等待中"
                          : "进行中"
                    : "待处理"}
                {current && p.total ? ` · ${p.processed}/${p.total}` : ""}
              </span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
