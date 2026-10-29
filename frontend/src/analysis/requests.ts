import { api } from "../api";
import type { TaskReceipt } from "../types";
import type { AnalysisSelection } from "../types";

// 只提交本次分析选择并返回回执，完整任务由任务跟踪模块随后读取。
export async function createAnalysis(
  videoId: string,
  selection: AnalysisSelection,
  signal?: AbortSignal,
) {
  return api<TaskReceipt>(
    `/videos/${videoId}/analyses`,
    {
      method: "POST",
      errorPolicy: "caller",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(selection),
      signal,
    },
  );
}
