// 优先使用后端阶段文案，兼容带计数后缀的任务阶段。
export function stageLabel(stage: string, labels: Record<string, string>) {
  const separator = stage.indexOf(":");
  const name = separator < 0 ? stage : stage.slice(0, separator);
  const detail = separator < 0 ? "" : stage.slice(separator + 1);
  return (labels[name] || name) + (detail ? name === "concise" ? ` · 第 ${detail} 段` : ` · ${detail}` : "");
}
