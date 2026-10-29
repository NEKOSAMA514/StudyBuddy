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
      <h3>笔记补充要求</h3>
      <button type="button" className="text-button" disabled={busy}
        onClick={() => setConfig({ ...config, summary_prompt: "" })}>清空补充要求</button>
    </div>
    <p>可留空，直接使用系统规则。需要时填写你希望笔记怎样组织、重点解释什么或采用什么表达方式，例如“先比较各概念，再解释操作步骤”。</p>
    <label htmlFor="summary-prompt">笔记补充要求（选填）</label>
    <textarea id="summary-prompt" maxLength={10000} rows={6} disabled={busy}
      placeholder="留空使用系统规则；例如：突出概念之间的区别，用简单例子解释关键步骤。"
      value={config.summary_prompt} onChange={(event) => setConfig({ ...config, summary_prompt: event.target.value })} />
    <small>只有填写并保存后才加入模型请求。固定系统规则负责事实、证据和输出格式；补充要求用于组织、解释和呈现。修改或清空后都需要保存，再点击“重新生成笔记”应用到已有视频；识别原文和截图继续复用。</small>
    <div className="settings-actions">
      <button className="primary" type="submit" disabled={busy}>{busy ? "正在保存…" : "保存笔记风格"}</button>
    </div>
  </form>;
}
