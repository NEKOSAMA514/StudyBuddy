import type { Task } from "../../src/types";

const stage_labels = { queued: "等待处理", checking: "检查视频", transcribing: "识别讲解语音", ocr: "提取画面文字",
  planning: "规划视频章节", summarizing: "生成章节正文", segments: "逐段生成笔记", finalizing: "生成整体概览与校验引用", completed: "分析完成" };
const timing_labels = { transcribing: "识别讲解语音", ocr: "提取画面文字", planning: "规划视频章节",
  summarizing: "生成章节正文", finalizing: "生成整体概览与校验引用", total: "本次总耗时" };
export function taskResponse(task: any): Task {
  if (task.notes_state === undefined || task.notes_revision === undefined) throw new Error("任务替身缺少笔记状态或版本");
  const keys = task.progress?.segmented ? ["checking", "transcribing", "ocr", "segments", "finalizing"]
    : ["checking", "transcribing", "ocr", "planning", "summarizing", "finalizing"];
  const current = keys.indexOf(task.stage.split(":")[0]);
  const done = task.status === "succeeded" ? keys.length : Math.max(0, current);
  return { attempts: 1, created_at: "2026-10-17T00:00:00Z", updated_at: "2026-10-17T00:00:00Z", segment_progress: null,
    ...task, progress: { percent: task.status === "succeeded" ? 100 : 0, completed_steps: done,
      current_step: Math.min(done + 1, keys.length), processed: 0, total: null, ...task.progress,
      steps: keys.map(key => ({ key, title: stage_labels[key as keyof typeof stage_labels], description: "测试步骤说明" })),
      stage_labels, timing_labels } };
}
