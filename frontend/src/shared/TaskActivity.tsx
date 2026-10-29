import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { ActivityError, TaskActivityData } from "../types";
import { taskStatusLabels, type TaskStatus } from "./taskStatus";

const phases: Record<string, string> = { pacing: "等待发送", waiting: "等待模型返回", receiving: "接收结果",
  validating: "校验结果", validated: "校验通过，等待保存", retry_wait: "自动重试等待", repair: "结构修正" };
const operations: Record<string, string> = { outline: "大纲", extract: "章节信息", chapters: "章节正文",
  overview: "概览", issue_quotes: "引文修正", concise: "简洁导览" };
const duration = (seconds: number) => `${Math.floor(seconds / 60).toString().padStart(2, "0")}:${Math.floor(seconds % 60).toString().padStart(2, "0")}`;

// 相同原因的图片警告合并展示并保留全部位置，带不同错误引用的消息分别保留。
function groupWarnings(warnings: string[]) {
  const groups = new Map<string, { message: string; reason: string; locations: string[] }>();
  for (const message of new Set(warnings)) {
    const image = /^(.+? · 图片\d+)：(.+)$/.exec(message);
    const key = image ? `image:${image[2]}` : `message:${message}`;
    const group = groups.get(key);
    if (group && image) group.locations.push(image[1]);
    else groups.set(key, { message, reason: image?.[2] || message, locations: image ? [image[1]] : [] });
  }
  return [...groups.values()];
}

function ErrorReference({ error, context = "" }: { error?: ActivityError | null; context?: string }) {
  if (!error) return null;
  return <div className="activity-error-reference">
    {error.message && !context.includes(error.message) && <span>{error.message}</span>}
    {error.error_id && <span>错误编号：{error.error_id}</span>}
    {error.log_file && <details><summary>详细日志</summary><p>项目文件夹中的 <code>{error.log_file}</code></p></details>}
  </div>;
}

// 当前阶段优先，历史动态次之，时间和日志位置弱化；复用主题与任务排版。
export function TaskActivity({ activity, status, stage, reconnecting, error, warnings }: {
  activity?: TaskActivityData | null; status?: TaskStatus; stage: string; reconnecting: boolean;
  error?: ActivityError | null; warnings: string[];
}) {
  const scroll = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  // 本地计时只补充显示等待时长，断线时冻结，不回写后端任务状态。
  const snapshot = useRef({ value: activity, at: performance.now() });
  const [unread, setUnread] = useState(false);
  const [, tick] = useState(0);
  if (snapshot.current.value !== activity) snapshot.current = { value: activity, at: performance.now() };
  const requests = status === "running" ? activity?.requests || [] : [];
  useEffect(() => {
    if (!requests.length || reconnecting) return;
    const timer = window.setInterval(() => tick(value => value + 1), 1000);
    return () => window.clearInterval(timer);
  }, [requests.length, reconnecting]);
  const sequence = activity?.sequence || 0;
  useLayoutEffect(() => {
    if (following.current && scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight;
    else setUnread(true);
  }, [sequence, error?.error_id, warnings.join("\n")]);
  let heading = !status ? stage : status !== "running" ? taskStatusLabels[status] : activity?.current?.message || stage;
  if (requests.length === 1) heading = phases[requests[0].state] || heading;
  if (requests.length > 1) {
    const counts = new Map<string, number>();
    for (const request of requests) counts.set(request.state, (counts.get(request.state) || 0) + 1);
    heading = [...counts].map(([state, count]) => `${count} 个请求${phases[state] || "处理中"}`).join("，");
  }
  const history = activity?.history || [];
  const finalError = error || activity?.final_error;
  const finalInHistory = finalError?.error_id && history.some(event => event.error?.error_id === finalError.error_id);
  const visibleWarnings = warnings.filter(message => !history.some(event => event.message === message ||
    event.error?.error_id && message.includes(event.error.error_id)));
  return <section className="task-activity" aria-label="任务动态">
    <div className="activity-heading"><strong>任务动态</strong><span role="status">{heading}</span></div>
    {reconnecting && <p className="activity-connection" role="status">暂时无法更新状态，正在重新连接；以下为最后已知进度。</p>}
    {!!requests.length && <ul className="activity-requests">{requests.map(request => <li key={request.id}>
      <span>{operations[request.operation] || "结果"}{request.chapter_ids.length > 0 && ` · ${request.chapter_ids.join("、")}`} · {phases[request.state] || "处理中"}</span>
      {["waiting", "receiving", "pacing", "retry_wait"].includes(request.state) && <span>
        {request.state === "receiving" ? "自发送起" : "已等待"} {duration(request.elapsed_seconds + (reconnecting ? 0 : (performance.now() - snapshot.current.at) / 1000))}
      </span>}
    </li>)}</ul>}
    <div className="activity-history" ref={scroll} tabIndex={0} aria-label="任务动态记录" onScroll={() => {
      const node = scroll.current;
      if (node) { following.current = node.scrollHeight - node.scrollTop - node.clientHeight <= 16; if (following.current) setUnread(false); }
    }}>
      {!!activity?.dropped && <p className="activity-muted">较早的 {activity.dropped} 条动态已省略。</p>}
      <ol>{history.map(event => <li key={event.id} className={`activity-event ${event.level}`}>
        <time dateTime={event.at}>{new Date(event.at).toLocaleTimeString("zh-CN", { hour12: false })}</time>
        <div><span>{event.level === "warning" ? "注意：" : event.level === "error" ? "错误：" : ""}{event.message}</span><ErrorReference error={event.error} context={event.message} /></div>
      </li>)}</ol>
      {!history.length && <p className="activity-muted">{stage}</p>}
      {finalError && !finalInHistory && <div className="activity-event error"><div><span>{finalError.message || "任务未能完成，请查看错误日志。"}</span><ErrorReference error={finalError} context={finalError.message} /></div></div>}
      {groupWarnings(visibleWarnings).map(group => group.locations.length > 1
        ? <details className="activity-warning" key={group.message}>
          <summary>注意：{group.reason}（共 {group.locations.length} 处）</summary>
          <p>涉及位置：{group.locations.join("；")}</p>
        </details>
        : <p className="activity-warning" key={group.message}>注意：{group.message}</p>)}
    </div>
    {unread && <button className="text-button activity-latest" onClick={() => {
      following.current = true; setUnread(false);
      if (scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight;
    }}>查看最新动态 ↓</button>}
  </section>;
}
