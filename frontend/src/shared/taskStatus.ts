export type TaskStatus = "queued" | "running" | "stopping" | "succeeded" | "failed" | "paused" | "interrupted";

export const taskStatusLabels: Record<TaskStatus, string> = {
  queued: "排队中", running: "处理中", stopping: "正在停止", succeeded: "已完成",
  failed: "处理失败", paused: "已停止", interrupted: "已中断",
};
// 这些状态停止自动任务轮询，继续或重试必须取得新的操作回执。
export const terminal = new Set<string>(["succeeded", "failed", "interrupted", "paused"]);
