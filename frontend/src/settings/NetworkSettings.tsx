import { notifySuccess } from "../notifications";
import { useState } from "react";
import { useSettingsForm } from "./useSettingsForm";

type Config = { enabled: boolean; url: string };

// 复用简单设置生命周期编辑下载代理，字段和保存后的说明由本页维护。
export function NetworkSettings() {
  const [message, setMessage] = useState("");
  const { draft: config, setDraft: setConfig, busy, save, reading } = useSettingsForm<Config>("/settings/network", () => {
    setMessage("已保存，用于新的解析和下载；正在运行的任务保持原配置。");
    notifySuccess("代理设置保存成功。");
  });
  if (!config) return reading.error ? <div className="settings-read-error" role="status"><span>{reading.error}</span><button type="button" className="secondary" onClick={reading.retry}>重新读取设置</button></div> : <p>正在读取设置…</p>;
  return <form className="settings-card" onSubmit={(event) => { event.preventDefault(); setMessage(""); void save(); }}>
    <h2>网络代理</h2>
    <p>关闭时所有视频平台直连。开启后所有视频平台使用此代理，国内外站点的分流由代理软件负责。</p>
    <label className="setting-check">
      <input type="checkbox" checked={config.enabled} disabled={busy} onChange={(event) => {
        setConfig({ ...config, enabled: event.target.checked }); setMessage("");
      }} />启用视频下载代理
    </label>
    <label>代理地址
      <input value={config.url} disabled={busy} required={config.enabled} placeholder="http://127.0.0.1:7890"
        autoComplete="off" spellCheck={false} onChange={(event) => {
          setConfig({ ...config, url: event.target.value }); setMessage("");
        }} />
    </label>
    <small>支持无认证的 HTTP、HTTPS、SOCKS5、SOCKS5H 代理。关闭后保留地址，视频下载以外的请求不受此设置影响。</small>
    <p>Windows 本机运行可填写 http://127.0.0.1:7890，端口以代理软件的设置为准。</p>
    {message && <p role="status" className="notice">{message}</p>}
    <div className="settings-actions"><button className="primary" type="submit" disabled={busy}>{busy ? "正在保存…" : "保存代理设置"}</button></div>
  </form>;
}
