import { notifySuccess } from "../notifications";
import { useSettingsForm } from "./useSettingsForm";
import type { NoteStyle } from "../types";
import { NoteStyleSelect } from "./NoteStyleSelect";

type Config = { summary_prompt: string; note_style: NoteStyle };

// 编辑默认风格与补充要求，草稿清空后仍需保存才影响后续任务。
export function NoteStyleSettings({ onSaved }: { onSaved: () => void }) {
  const { draft: config, setDraft: setConfig, busy, save, reading } = useSettingsForm<Config>("/settings/note-style", () => {
    onSaved();
    notifySuccess("笔记风格保存成功。");
  });
  if (!config) return reading.error ? <div className="settings-read-error" role="status"><span>{reading.error}</span><button type="button" className="secondary" onClick={reading.retry}>重新读取设置</button></div> : <p>正在读取设置…</p>;
  return <form className="settings-card" onSubmit={(event) => { event.preventDefault(); void save(); }}>
    <NoteStyleSelect label="默认笔记风格" value={config.note_style} disabled={busy}
      onChange={note_style => setConfig({ ...config, note_style })} />
    <div className="prompt-heading">
      <label htmlFor="summary-prompt">笔记补充要求（选填）</label>
      <button type="button" className="text-button" disabled={busy}
        onClick={() => setConfig({ ...config, summary_prompt: "" })}>清空补充要求</button>
    </div>
    <textarea id="summary-prompt" maxLength={10000} rows={6} disabled={busy} aria-describedby="summary-prompt-help"
      placeholder="例如：用一个例子解释关键步骤。"
      value={config.summary_prompt} onChange={(event) => setConfig({ ...config, summary_prompt: event.target.value })} />
    <small className="settings-help" id="summary-prompt-help">留空使用默认规则。补充要求可调整笔记的组织和讲解方式。</small>
    <div className="settings-actions">
      <button className="primary" type="submit" disabled={busy} aria-describedby="note-style-save-help">{busy ? "正在保存…" : "保存笔记风格"}</button>
    </div>
    <small className="settings-help" id="note-style-save-help">保存后用于新任务。已有资料需重新生成笔记才会应用。</small>
  </form>;
}
