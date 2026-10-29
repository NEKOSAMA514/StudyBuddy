import { useCallback, useEffect, useRef, useState } from "react";
import { ReportedError, isAbortError, reportClientError } from "./api";
import { notifyError, subscribeNotifications, type Notification } from "./notifications";
import { ErrorNotification } from "./diagnostics";

// 展示通知并对未登记异常补报日志，序号阻止迟到的报告覆盖新通知。
export function useNotifications() {
  const [notice, setNotice] = useState<Notification | null>(null);
  const [occurrence, setOccurrence] = useState(0);
  const sequence = useRef(0);
  const onError = useCallback((value: unknown) => {
    if (isAbortError(value)) return;
    const message = value instanceof Error ? value.message : String(value);
    if (!message) {
      setOccurrence(++sequence.current);
      setNotice(null);
      return;
    }
    notifyError(message);
    const version = sequence.current;
    if (message && !(value instanceof ReportedError)) {
      void reportClientError(value, message).then(reported => {
        if (version === sequence.current) setNotice({ kind: "error", message: reported });
      });
    }
  }, []);
  useEffect(() => subscribeNotifications(value => {
    // 请求错误已经登记，订阅端只负责展示，不重复上报。
    setOccurrence(++sequence.current);
    setNotice(value);
  }), []);
  return { onError, notification: <ErrorNotification message={notice?.message || ""} kind={notice?.kind}
    link={notice?.link} occurrence={occurrence} onClose={() => onError("")} /> };
}
