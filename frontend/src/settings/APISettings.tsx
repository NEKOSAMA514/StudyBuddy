import { useAPISettings, outputStages } from "./useAPISettings";
import { apiProviders } from "./apiProviders";

// 主操作是保存连接；厂商、密钥和模型优先，自定义地址与高级参数按需展示。
// 沿用现有表单间距、语义颜色及明暗主题，不增加独立控件样式。
// 展示多组连接及当前草稿，把保存、启用、模型读取与测试交给专用 Hook。
export function APISettings({ onSaved }: { onSaved: (close: boolean) => void }) {
  const { saved, editing, update, providerId, changeProvider, token, changeToken, models, manualModel, setManualModel, busy, message, failed,
    select, create, save, readModels, test, activate, remove, original, hasCredential } = useAPISettings(onSaved);
  if (!saved || !editing) return <p>{failed ? "无法读取 API 配置，请刷新设置后重试。" : "正在读取 API 配置…"}</p>;
  const provider = apiProviders.find(p => p.id === providerId);
  const needsEndpoint = providerId === "custom" || !provider?.baseURL;
  return <>
    <div className="api-profile-heading"><h2>LLM API 连接</h2>
      <button type="button" disabled={busy} onClick={create}>添加配置</button>
    </div>
    <div className="api-profile-list" aria-label="已保存的 API 配置">
      {saved.profiles.map(profile => <button type="button" key={profile.id} disabled={busy}
        className="api-profile" aria-pressed={editing.id === profile.id} onClick={() => select(profile)}>
        <span><strong>{profile.name}</strong>{profile.id === saved.active_id && <span className="api-active">当前使用</span>}
          {editing.id === profile.id && <span className="api-selected">✓ 正在编辑</span>}</span>
        <small>{profile.model}</small>
      </button>)}
    </div>
    {message && <p role="status" className="notice">{message}</p>}
    <form onSubmit={event => { event.preventDefault(); void save(); }}>
      <fieldset className="settings-card api-profile-form" disabled={busy}>
        <legend>{editing.id ? "编辑连接" : "添加连接"}</legend>
        <label>服务商<select aria-label="服务商" value={providerId} onChange={e => changeProvider(e.target.value)}>
          {apiProviders.map(provider => <option key={provider.id} value={provider.id} disabled={provider.disabled}>{provider.name}</option>)}
          <option value="custom">自定义接口</option>
        </select></label>
        {!needsEndpoint && <small>已预设官方 API 地址，无需填写。</small>}
        <label>配置名称<input required maxLength={80} value={editing.name}
          placeholder="例如：魔搭、公司网关、本地模型" onChange={e => update({ name: e.target.value })} /></label>
        {needsEndpoint && <><label>API 地址<input required type="url" maxLength={2048} value={editing.base_url}
          placeholder={provider?.endpointPlaceholder ?? "https://你的服务地址/v1"} onChange={e => update({ base_url: e.target.value })} /></label>
        <small>支持 Chat Completions 兼容接口。填写基础地址，程序会追加 /chat/completions；远程使用 HTTPS，本机可用 HTTP。</small></>}
        <label>API 密钥<input type="password" autoComplete="new-password" maxLength={4096} value={token}
          placeholder={hasCredential && !token ? "已保存；留空保留，输入新值替换" : "填写这组连接的密钥"}
          onChange={e => changeToken(e.target.value)} /></label>
        <small>{hasCredential ? "密钥已填写或已保存" : "尚未填写这组连接的密钥"}；切换服务商或修改 API 地址时需重新填写。</small>
        <label>模型 ID{models.length > 0 && !manualModel ? <select aria-label="模型 ID" required
          value={editing.model} onChange={e => update({ model: e.target.value })}>
          <option value="" disabled>请选择模型（共 {models.length} 个）</option>
          {editing.model && !models.includes(editing.model) && <option value={editing.model}>{editing.model}（当前填写）</option>}
          {models.map(model => <option key={model} value={model}>{model}</option>)}
        </select> : <input required maxLength={200} value={editing.model}
          placeholder="填写密钥后读取可用模型，或手动输入模型 ID" onChange={e => update({ model: e.target.value })} />}</label>
        <div className="api-model-actions">
          {models.length > 0 && <button type="button" onClick={() => setManualModel(!manualModel)}>
            {manualModel ? "从列表选择" : "手动输入"}
          </button>}
          <button type="button" disabled={!hasCredential || !editing.base_url} onClick={() => void readModels()}>读取可用模型</button>
        </div>
        <small>使用当前表单的地址和密钥读取，不会自动保存；接口不支持列表时可手动填写模型 ID。</small>
        <details className="api-advanced"><summary>高级设置</summary>
          <label>并发请求数<select aria-label="并发请求数" value={editing.section_concurrency} onChange={e => update({ section_concurrency: Number(e.target.value) })}>
            {[1, 2, 3, 4].map(n => <option key={n} value={n}>{n}</option>)}
          </select></label>
          <label>请求发送间隔（秒）<input type="number" required min={1} max={60} step={1} value={editing.request_interval_seconds}
            onChange={e => update({ request_interval_seconds: e.target.value === "" ? "" : Number(e.target.value) })} /></label>
          <small>默认并发 3、间隔 3 秒。重试与格式修正共用发送间隔；按服务商限流调整。</small>
          <fieldset className="api-budget"><legend>输入上限（字符）</legend>
            <label>生成输入上限（字符）<input type="number" required min={1} step={1} value={editing.input_text_limit}
              onChange={e => update({ input_text_limit: e.target.value === "" ? "" : Number(e.target.value) })} /></label>
            <small>统计系统提示词及 Schema、标题、补充要求、原文、大纲、上下文和修正时重发的正文；不计 JSON 包装、字段名及来源定位编号，也不计尚未生成的正文。输出上限仍以 Token 计。</small>
          </fieldset>
          <fieldset className="api-budget"><legend>各阶段输出上限（Token）</legend>
            {outputStages.map(([key, label]) => <label key={key}>{label}输出上限<input type="number" required min={1} step={1}
              value={editing.output_tokens[key]} onChange={e => update({ output_tokens: {
                ...editing.output_tokens, [key]: e.target.value === "" ? "" : Number(e.target.value),
              } })} /></label>)}
            <small>这是单次请求的最大输出量，不是目标篇幅；请按模型支持范围填写。保存后用于新任务，继续／重试沿用原任务参数。</small>
          </fieldset>
          <label className="setting-check"><input type="checkbox" checked={editing.stream} onChange={e => update({ stream: e.target.checked })} />使用流式响应</label>
          <label className="setting-check"><input type="checkbox" checked={editing.json_mode} onChange={e => update({ json_mode: e.target.checked })} />启用接口 JSON 模式（需模型支持）</label>
        </details>
        <div className="settings-actions">
          <button className="primary" type="submit">保存配置</button>
          <button type="button" disabled={!hasCredential || !editing.base_url || !editing.model} onClick={() => void test()}>测试连接</button>
          {editing.id && <button type="button" disabled={editing.id === saved.active_id} onClick={() => void activate()}>启用此配置</button>}
        </div>
        <small>测试连接会发送一条简短消息，可能产生费用；保存不会调用模型。启用时使用已保存的内容。</small>
        {editing.id && <div className="settings-actions">
          <button type="button" disabled={!original?.has_token} onClick={() => void save(true)}>删除已保存密钥</button>
          <button type="button" disabled={editing.id === saved.active_id} onClick={() => void remove()}>删除配置</button>
        </div>}
        {editing.id === saved.active_id && <small>删除当前配置前，请先启用另一组配置。</small>}
      </fieldset>
    </form>
  </>;
}
