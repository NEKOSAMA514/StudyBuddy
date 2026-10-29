import { notifyError } from "./notifications";
const API_BASE = "/api/v1";
export const apiUrl = (path: string) => API_BASE + path;

export type ErrorReference = {
  message?: string;
  error_id?: string;
  log_file?: string;
  log_saved?: boolean;
};
export class ReportedError extends Error {
  constructor(message: string, readonly status?: number) { super(message); }
}

export function isAbortError(error: unknown) {
  return error instanceof Error && error.name === "AbortError";
}

// 将后端错误和日志引用组合为可展示文案，不暴露原始响应对象。
export function errorText(
  error: ErrorReference,
  fallback = "出现了未知错误，请查看错误报告。",
) {
  const message = error.message || fallback;
  if (error.log_saved === false)
    return `${message}\n详细日志写入失败，请检查项目 log 文件夹的权限和磁盘空间。错误编号：${error.error_id}。`;
  return error.log_file
    ? `${message}\n详细日志已保存到项目文件夹中的 ${error.log_file}（错误编号：${error.error_id}）。`
    : message;
}

type RequestContext = {
  code?: "request_network_error" | "response_format_error" | "http_error";
  request_url?: string;
  request_method?: string;
  http_status?: number;
  response_body?: string;
};
// 后台轮询及可选配置读取由调用方显示连接状态，不重复通知或上报同一连接故障。
export type RequestErrorPolicy = "automatic" | "caller" | "silent";
type RequestOptions = RequestInit & { errorPolicy?: RequestErrorPolicy };
// 只补报尚无后端错误引用的异常，上报失败时仍返回可读提示。
export async function reportClientError(
  error: unknown,
  message = "页面出现错误，请查看错误报告。",
  context: RequestContext = {},
) {
  if (error instanceof ReportedError) return error.message;
  if (isAbortError(error)) return "";
  try {
    const response = await fetch(apiUrl("/diagnostics/client-errors"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: message.slice(0, 2000),
        stack:
          error instanceof Error ? error.stack || error.message : String(error),
        location: window.location.pathname + window.location.hash,
        ...context,
      }),
    });
    if (!response.ok) throw new Error("日志服务不可用");
    const reference = await response.json();
    if (
      typeof reference?.error_id !== "string" ||
      typeof reference?.log_saved !== "boolean"
    )
      throw new Error("日志服务返回异常");
    return errorText({ ...reference, message: reference.message || message });
  } catch {
    return `${message}\n本地服务未连接或日志写入失败，详细日志暂未保存。请先检查 StudyBuddy 本机服务。`;
  }
}

// 集中处理请求、取消和响应解析，由错误策略决定上报与通知责任。
export async function requestJSON<T>(
  url: string,
  options?: RequestOptions,
): Promise<T> {
  const { errorPolicy = "automatic", ...init } = options || {};
  let response: Response;
  let text: string;
  const context = { request_url: url, request_method: init?.method || "GET" };
  try {
    response = await fetch(url, init);
    text = await response.text();
  } catch (error) {
    return requestConnectionError(error, url, context.request_method, init.signal || undefined, errorPolicy);
  }
  return decodeJSONResponse<T>(text, response.status, url, context.request_method, init?.signal || undefined, errorPolicy);
}

export async function requestConnectionError(error: unknown, url: string, method: string, signal?: AbortSignal, errorPolicy: RequestErrorPolicy = "automatic"): Promise<never> {
  signal?.throwIfAborted();
  if (isAbortError(error)) throw error;
  const fallback = "无法连接本地服务，请检查服务是否运行。";
  const message = errorPolicy === "silent" ? fallback : await reportClientError(error, fallback,
    { request_url: url, request_method: method, code: "request_network_error" });
  signal?.throwIfAborted();
  if (errorPolicy === "automatic") notifyError(message);
  throw new ReportedError(message);
}

// 普通请求与上传共用解析规则，格式错误和 HTTP 错误都保留统一引用。
export async function decodeJSONResponse<T>(text: string, status: number, url: string, method: string, signal?: AbortSignal, errorPolicy: RequestErrorPolicy = "automatic"): Promise<T> {
  signal?.throwIfAborted();
  const response = { ok: status >= 200 && status < 300, status };
  const context = { request_url: url, request_method: method };
  let body;
  try {
    body = JSON.parse(text);
  } catch (error) {
    if (isAbortError(error)) throw error;
    if (errorPolicy === "silent") throw new ReportedError("服务返回的数据格式异常，请稍后重试。", status);
    const message = await reportClientError(
      error,
      response.ok
        ? "服务返回的数据格式异常，请查看错误报告。"
        : "请求未能完成，请查看错误报告。",
      {
        ...context,
        http_status: response.status,
        response_body: text,
        code: response.ok ? "response_format_error" : "http_error",
      },
    );
    signal?.throwIfAborted();
    if (errorPolicy === "automatic") notifyError(message);
    throw new ReportedError(message, status);
  }
  if (!response.ok) {
    const message =
      typeof body?.error?.error_id === "string"
        ? errorText(body.error)
        : errorPolicy === "silent" ? "请求未能完成，请稍后重试。"
        : await reportClientError(
            new Error("HTTP " + response.status),
            "请求未能完成，请查看错误报告。",
            {
              ...context,
              http_status: response.status,
              response_body: text,
              code: "http_error",
            },
          );
    signal?.throwIfAborted();
    if (errorPolicy === "automatic") notifyError(message);
    throw new ReportedError(message, status);
  }
  return body;
}

// 补报未捕获异常和未处理的异步失败，已登记错误与主动取消不重复通知。
export function installErrorReporting() {
  const report = (error: unknown) => {
    if (error instanceof ReportedError || isAbortError(error)) return;
    void reportClientError(error).then(notifyError);
  };
  window.addEventListener("error", (event) =>
    report(event.error || event.message),
  );
  window.addEventListener("unhandledrejection", (event) =>
    report(event.reason),
  );
}

export function api<T>(path: string, init?: RequestOptions): Promise<T> {
  return requestJSON<T>(apiUrl(path), init);
}

export const evidenceImageUrl = (id: string) => apiUrl(`/evidence/${id}/image`);
export const notesUrl = (id: string, format: "txt" | "md" = "txt") =>
  apiUrl(`/analyses/${id}/notes${format === "md" ? "?format=md" : ""}`);
export const transcriptUrl = (
  videoId: string,
  taskId: string,
  format: "txt" | "json",
) => apiUrl(`/videos/${videoId}/transcript?task_id=${taskId}&format=${format}`);
