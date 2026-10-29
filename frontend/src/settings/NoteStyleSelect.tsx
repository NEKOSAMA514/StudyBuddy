import type { NoteStyle } from "../types";

// 共用详尽与简洁风格选项，当前选择和禁用状态由表单持有。
export function NoteStyleSelect({ value, onChange, disabled, label = "笔记风格" }: {
  value: NoteStyle; onChange: (value: NoteStyle) => void; disabled?: boolean; label?: string;
}) {
  return <label className="note-style-selection">{label}
    <select value={value} disabled={disabled} onChange={event => onChange(event.target.value as NoteStyle)}>
      <option value="detailed">详尽</option>
      <option value="concise">简洁</option>
    </select>
    <small>{value === "concise" ? "直接整理成短文，每段建议约 2000 字，不设硬上限。" : "先规划章节再展开，保留必要解释与细节，表达精练，不限制篇幅。"}</small>
  </label>;
}
