import { useCallback, useEffect, useState } from "react";
import { api } from "../api";

// 按资料身份隔离可重试读取，同一身份刷新保留旧数据，切换时取消旧请求。
export function useScopedRead<T>({ identity, path, refreshKey, enabled = true, errorMessage, onError }: {
  identity: string; path: string; refreshKey?: string | number; enabled?: boolean;
  errorMessage: string; onError: (error: unknown) => void;
}) {
  const [saved, setSaved] = useState<{ identity: string; data: T } | null>(null);
  const [attempt, setAttempt] = useState(0);
  const key = JSON.stringify([identity, path, refreshKey, attempt]);
  const [request, setRequest] = useState<{ key: string; loading: boolean; error: string | null } | null>(null);
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    setRequest({ key, loading: true, error: null });
    api<T>(path, { errorPolicy: "caller", signal: controller.signal }).then(data => {
      if (controller.signal.aborted) return;
      setSaved({ identity, data });
      setRequest({ key, loading: false, error: null });
    }).catch(error => {
      if (controller.signal.aborted) return;
      setRequest({ key, loading: false, error: errorMessage });
      onError(error);
    });
    return () => controller.abort();
  }, [identity, path, key, enabled, errorMessage, onError]);
  return {
    data: enabled && saved?.identity === identity ? saved.data : null,
    loading: enabled && (request?.key !== key || request.loading),
    error: enabled && request?.key === key ? request.error : null,
    // 只有本轮成功响应才算确认，调用方不能用缓存或失败结果判定版本淘汰。
    confirmed: enabled && saved?.identity === identity && request?.key === key && !request.loading && !request.error,
    retry: useCallback(() => setAttempt(value => value + 1), []),
  };
}
