import { useEffect, useRef, useState } from "react";
import { readBilibiliSession, logoutBilibiliSession, publishBilibiliSession } from "../downloads/bilibiliSession";
import type { BilibiliSession } from "../types";
import { BilibiliLogin } from "../BilibiliLogin";

// 登录是未登录卡片的主操作，退出和重读为次操作；复用右侧操作区、主题颜色及原扫码弹窗。
// 管理 B 站会话读取、扫码与退出，状态同步通过明确的会话模块发布。
export function CredentialSettings() {
  const [session, setSession] = useState<BilibiliSession | null>(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const [message, setMessage] = useState("");
  const [loginOpen, setLoginOpen] = useState(false);
  const lifetime = useRef<AbortController | null>(null);

  async function read(signal: AbortSignal) {
    setBusy(true);
    setFailed(false);
    try {
      const result = await readBilibiliSession(signal);
      if (!signal.aborted) setSession(result);
    } catch { if (!signal.aborted) setFailed(true); }
    finally { if (!signal.aborted) setBusy(false); }
  }

  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    void read(controller.signal);
    return () => controller.abort();
  }, []);

  // 仅在服务器确认退出后更新会话，失败保留当前登录状态。
  async function logout() {
    const signal = lifetime.current!.signal;
    setBusy(true);
    setMessage("");
    try {
      const result = await logoutBilibiliSession(signal);
      if (signal.aborted) return;
      setSession(result);
      setMessage("已退出 B 站登录，并清除本机登录凭证。");
    } catch { /* 请求失败保留登录状态，由 requestJSON 提示。 */ }
    finally { if (!signal.aborted) setBusy(false); }
  }

  return <section className="settings-card" aria-label="登录凭证管理">
    <h2>登录凭证管理</h2>
    <p>查看已接入网站的登录状态，退出后将清除 StudyBuddy 保存的对应凭证。</p>
    <div className="credential-site">
      <div><h3>B 站</h3>
        <p>{!session ? failed ? "登录状态读取失败" : "正在读取登录状态…" :
          session.state === "logged_in" ? "已登录" : session.state === "expired" ? "登录已失效" :
          session.state === "unknown" ? "暂时无法确认登录状态" : "未登录"}</p>
        {session?.state === "logged_in" && <small>{session.persistent ? "登录凭证已加密保存到本机" : "登录凭证仅本次运行有效"}</small>}
        {session?.warning && <p>{session.warning}</p>}
      </div>
      <div className="credential-actions">
        {(session?.state === "logged_out" || session?.state === "expired") && <button type="button" className="primary" disabled={busy}
          onClick={() => { setMessage(""); setLoginOpen(true); }}>扫码登录</button>}
        {session && session.state !== "logged_out" && <button type="button" className="secondary" disabled={busy} onClick={() => void logout()}>{busy ? "正在处理…" : "退出登录"}</button>}
        {(failed || session?.state === "unknown") && <button type="button" className="secondary" disabled={busy} onClick={() => void read(lifetime.current!.signal)}>重新读取</button>}
      </div>
    </div>
    {message && <p role="status">{message}</p>}
    {loginOpen && <BilibiliLogin immediate canContinue={false} session={session}
      onClose={() => setLoginOpen(false)} onContinue={() => setLoginOpen(false)}
      onSuccess={result => {
        setSession(result); setLoginOpen(false); setMessage("已登录 B 站。");
        publishBilibiliSession(result);
      }} />}
  </section>;
}
