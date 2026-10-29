import type { BilibiliSession } from "./types";
import { useEffect, useRef, useState } from "react";
import { apiUrl, requestJSON } from "./api";

type LoginState = "choice" | "creating" | "waiting" | "scanned" | "expired" | "unknown" | "failed" | "success" | "cancelled";
type LoginResponse = { id: string; state: Exclude<LoginState, "choice">; image?: string; session?: BilibiliSession };
const messages: Record<LoginState, string> = {
  choice: "", creating: "正在获取二维码…", waiting: "请用 B 站 App 扫码登录",
  scanned: "已扫码，请在手机上确认", expired: "二维码已过期，请刷新",
  unknown: "暂时无法确认登录状态，正在重试…", failed: "登录未完成，请重试",
  success: "登录成功", cancelled: "本次登录已取消，请重试",
};

// 展示一次扫码登录流程，成功后将服务器确认的会话交给调用方。
export function BilibiliLogin({ immediate, canContinue, session, onClose, onContinue, onSuccess }: {
  immediate: boolean; canContinue: boolean; session: BilibiliSession | null;
  onClose: () => void; onContinue: () => void; onSuccess: (session: BilibiliSession) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const control = useRef<{ id?: string; controller?: AbortController; timer?: number; version: number }>({ version: 0 });
  const [state, setState] = useState<LoginState>("choice");
  const [image, setImage] = useState("");

  // 关闭或刷新二维码时取消旧轮询，并尽力释放对应的后端登录尝试。
  function cancel() {
    const current = control.current;
    current.version++;
    current.controller?.abort();
    window.clearTimeout(current.timer);
    if (current.id) void fetch(apiUrl("/bilibili/login/" + current.id), { method: "DELETE", keepalive: true }).catch(() => {});
    current.id = undefined;
  }

  // 为每次扫码建立独立编号和请求代次，旧二维码结果不能覆盖新流程。
  async function start() {
    cancel();
    const current = control.current;
    const version = current.version;
    const id = crypto.randomUUID().replaceAll("-", "");
    const controller = new AbortController();
    current.id = id;
    current.controller = controller;
    setState("creating");
    setImage("");
    const valid = () => control.current.version === version && !controller.signal.aborted;
    async function poll() {
      try {
        const result = await requestJSON<LoginResponse>(apiUrl("/bilibili/login/" + id + "/poll"), {
          method: "POST", signal: controller.signal,
        });
        if (!valid()) return;
        setState(result.state);
        if (result.state === "success" && result.session) {
          onSuccess(result.session);
        } else if (["waiting", "scanned", "unknown"].includes(result.state)) {
          current.timer = window.setTimeout(() => void poll(), 2000);
        }
      } catch {
        if (valid()) setState("failed");
      }
    }
    try {
      const result = await requestJSON<LoginResponse>(apiUrl("/bilibili/login"), {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id }),
        signal: controller.signal,
      });
      if (!valid()) return;
      setState(result.state);
      setImage(result.image || "");
      if (result.state === "waiting") current.timer = window.setTimeout(() => void poll(), 1000);
    } catch {
      if (valid()) setState("failed");
    }
  }

  useEffect(() => {
    const element = dialog.current!;
    element.showModal();
    if (immediate) void start();
    return () => { cancel(); element.close(); };
    // 此弹窗独立持有本次登录；关闭和重新打开会建立新的会话。
  }, []);

  return <dialog ref={dialog} className="settings-dialog bilibili-login-dialog" aria-label="B 站登录"
    onCancel={event => { event.preventDefault(); onClose(); }}>
    <button type="button" className="dialog-close" aria-label="关闭 B 站登录" onClick={onClose}>×</button>
    <h2>扫码登录B站，使用AI字幕无需本地语音转写</h2>
    <p>Cookie 仅用于登录验证和获取平台字幕。字幕不可用时使用本地语音转写。</p>
    {session?.can_persist === false && <p>当前系统仅支持本次运行登录，重启后需重新扫码。</p>}
    {image && <img className="bilibili-login-qr" src={image} alt="B 站登录二维码" />}
    {state !== "choice" && <p role="status">{messages[state]}</p>}
    <div className="bilibili-login-actions">
      {state === "choice" ? <button type="button" className="primary" onClick={() => void start()}>扫码登录</button>
        : <button type="button" className="secondary" disabled={state === "creating"} onClick={() => void start()}>
          {["expired", "waiting", "scanned", "unknown"].includes(state) ? "刷新二维码" : "重新扫码"}
        </button>}
      {canContinue && <button type="button" className="secondary" onClick={onContinue}>暂不登录</button>}
      <button type="button" className="secondary" onClick={onClose}>取消</button>
    </div>
  </dialog>;
}
