import { useEffect, useRef, useState } from "react";
import { api, isAbortError } from "../api";

import { mineruTokenURL } from "../documents/mineru";

// 独立读取和保存 MinerU 凭据，失败时保留重读入口与常驻错误说明。
export function MinerUSettings({ onSaved }: { onSaved: () => void }) {
  const [saved, setSaved] = useState<{ has_token: boolean } | null>(null);
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const lifetime = useRef(new AbortController());
  const pending = useRef(false);
  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    api<{ has_token: boolean }>("/settings/mineru", { signal: controller.signal }).then(value => {
      if (!controller.signal.aborted) setSaved(value);
    }).catch(error => {
      if (!controller.signal.aborted && !isAbortError(error)) { setFailed(true); setMessage(error.message); }
    });
    return () => controller.abort();
  }, [attempt]);
  // 同步防重并绑定表单生命周期，仅成功后清空输入并通知设置已更新。
  async function save(clear = false) {
    const signal = lifetime.current.signal;
    if (pending.current || signal.aborted) return;
    pending.current = true;
    setBusy(true); setMessage(""); setFailed(false);
    try {
      const value = await api<{ has_token: boolean }>("/settings/mineru", { method: "PUT", signal,
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token, clear_token: clear }) });
      if (signal.aborted) return;
      setSaved(value); setToken(""); setMessage(clear ? "MinerU Token 已清除。" : "MinerU 设置已保存。");
      onSaved();
    } catch (error) {
      if (!signal.aborted && !isAbortError(error)) { setFailed(true); setMessage(error instanceof Error ? error.message : String(error)); }
    } finally { pending.current = false; if (!signal.aborted) setBusy(false); }
  }
  return <form className="settings-card mineru-settings" aria-labelledby="mineru-settings-title"
    onSubmit={event => { event.preventDefault(); if (saved) void save(); }}>
    <h2 id="mineru-settings-title">MinerU文档解析辅助</h2>
    <p>无 Token 使用轻量解析；保存 Token 后，新导入文档默认使用精准解析。</p>
    <a href={mineruTokenURL} target="_blank" rel="noopener noreferrer">免费申请 MinerU API Token ↗</a>
    {saved ? <>
      <label>MinerU API Token<input type="password" autoComplete="off" disabled={busy} value={token}
        placeholder={saved.has_token ? "已保存；留空保留，输入新值替换" : "填写 MinerU Token"} onChange={event => setToken(event.target.value)} /></label>
      <div className="settings-actions"><button disabled={busy}>保存 MinerU 设置</button>
        <button type="button" disabled={busy || !saved.has_token} onClick={() => void save(true)}>清除 MinerU Token</button></div>
    </> : message ? <div className="settings-read-error"><button type="button" className="secondary" onClick={() => { setMessage(""); setFailed(false); setAttempt(value => value + 1); }}>重新读取 MinerU 设置</button></div> : <p role="status">正在读取 MinerU 设置…</p>}
    {message && <p className="notice" role={failed ? "alert" : "status"}>{message}</p>}
  </form>;
}
