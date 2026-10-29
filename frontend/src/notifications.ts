export type NotificationLink = { href: string; label: string };
export type Notification = { kind: "error" | "success" | "info"; message: string; link?: NotificationLink };
const events = { error: "studybuddy-error", success: "studybuddy-success", info: "studybuddy-info" };

// 通知类别、消息和业务链接统一经此入口发布，展示层不再自行上报。
export function publishNotification(notice: Notification) {
  window.dispatchEvent(new CustomEvent<Notification>(events[notice.kind], { detail: notice }));
}
export const notifyError = (message: string) => publishNotification({ kind: "error", message });
export const notifySuccess = (message: string) => publishNotification({ kind: "success", message });
export const notifyInfo = (message: string, link?: NotificationLink) => publishNotification({ kind: "info", message, link });
// 封装所有通知事件的订阅，并返回卸载时使用的清理函数。
export function subscribeNotifications(receive: (notice: Notification) => void) {
  const listener = (event: Event) => receive((event as CustomEvent<Notification>).detail);
  Object.values(events).forEach(name => window.addEventListener(name, listener));
  return () => Object.values(events).forEach(name => window.removeEventListener(name, listener));
}
