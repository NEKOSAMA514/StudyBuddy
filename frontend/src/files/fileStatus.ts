import { taskStatusLabels } from "../shared/taskStatus";
import type { LibraryFile } from "../types";

export function fileStatusLabel(file: LibraryFile) {
  const status = file.latest_task?.status;
  if (status === "succeeded") return "笔记已生成";
  if (status) return taskStatusLabels[status];
  if (file.kind === "document") return file.extracted === false ? "待处理" : "已提取";
  return "未生成笔记";
}
