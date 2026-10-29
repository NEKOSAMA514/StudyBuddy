import type { NotificationLink } from "./notifications";
import { Component, useEffect, useRef, useState, type ErrorInfo, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { reportClientError } from "./api";

// 每次新通知重新计时，悬停暂停消失，手动关闭立即结束展示。
function useErrorVisibility(message: string, occurrence: string | number) {
  const [visible, setVisible] = useState(Boolean(message));
  const [hovered, setHovered] = useState(false);
  // 交替动画名重启渐隐，保留节点以维持悬停和离开事件。
  const [alternate, setAlternate] = useState(false);
  useEffect(() => {
    setVisible(Boolean(message));
    setAlternate(value => !value);
    if (!message) setHovered(false);
  }, [message, occurrence]);
  useEffect(() => {
    if (!message || hovered) return;
    const timer = window.setTimeout(() => setVisible(false), 5000);
    return () => window.clearTimeout(timer);
  }, [message, occurrence, hovered]);
  return { visible, dismiss: () => { setVisible(false); setHovered(false); },
    onMouseEnter: () => setHovered(true), onMouseLeave: () => setHovered(false),
    className: `fading-error${alternate ? " alternate" : ""}${hovered ? " hovered" : ""}` };
}

// 将通知挂到当前弹窗并提升到浏览器顶层，业务链接由发布方提供。
export function ErrorNotification({ message, onClose, kind = "error", occurrence = message, link }: {
  message: string; onClose?: () => void; kind?: "error" | "success" | "info"; occurrence?: string | number; link?: NotificationLink;
}) {
  const fade = useErrorVisibility(message, occurrence);
  const notification = useRef<HTMLDivElement>(null);
  const container = document.querySelector("dialog[open]") || document.body;
  useEffect(() => {
    const element = notification.current;
    if (!message || !element?.isConnected) return;
    // popover 进入浏览器顶层，设置 dialog 打开时也不会被遮住。
    element.showPopover();
    return () => { if (element.isConnected) element.hidePopover(); };
  }, [Boolean(message), container, fade.visible]);
  if (!message || !fade.visible) return null;
  return createPortal(
    <div ref={notification} popover="manual" role={kind === "success" ? "status" : "alert"}
      className={`error-notification ${kind !== "error" ? kind + " " : ""}${fade.className}`}
      onMouseEnter={fade.onMouseEnter} onMouseLeave={fade.onMouseLeave}>
      <div><strong>{kind === "success" ? "操作成功" : kind === "info" ? "提示" : "操作未完成"}</strong><p>{message}</p>
        {link && <a href={link.href} target="_blank" rel="noopener noreferrer">{link.label}</a>}</div>
      <button type="button" aria-label={kind === "success" ? "关闭成功通知" : "关闭错误通知"} onClick={() => { fade.dismiss(); notification.current?.hidePopover(); onClose?.(); }}>
        <svg viewBox="0 0 20 20" aria-hidden="true"><path d="m5 5 10 10M15 5 5 15" /></svg>
      </button>
    </div>,
    container,
  );
}

// 捕获渲染错误并登记诊断，保留可刷新页面而不是让整个应用空白。
export class ErrorBoundary extends Component<{ children: ReactNode }, { message: string }> {
  state = { message: "" };
  static getDerivedStateFromError() {
    return { message: "页面出现问题，正在保存错误日志…" };
  }
  componentDidCatch(error: Error, info: ErrorInfo) {
    const detail = new Error(error.message);
    detail.stack = `${error.stack}\n${info.componentStack}`;
    void reportClientError(detail).then((message) => this.setState({ message }));
  }
  render() {
    return this.state.message ? (
      <main className="crash-page">
        <h1>StudyBuddy</h1>
        <ErrorNotification message={this.state.message} />
        <p>页面出现问题，请查看顶部通知中的日志位置后刷新。</p>
        <button onClick={() => window.location.reload()}>刷新页面</button>
      </main>
    ) : this.props.children;
  }
}
