import { notifySuccess } from "../notifications";
import type { APIProfileSummary } from "./generationSettings";
import { useEffect, useRef, useState } from "react";
import { api, isAbortError, ReportedError } from "../api";

export const outputStages = [
  ["outline", "章节规划"], ["extract", "信息提取"], ["chapters", "正文生成（详尽／简洁）"],
  ["overview", "整体概览"], ["issue_quotes", "存疑引文修正"], ["connection_test", "连接测试"],
] as const;
type OutputTokens = Record<typeof outputStages[number][0], number>;

type Profile = APIProfileSummary & {
  base_url: string;
  stream: boolean; json_mode: boolean; section_concurrency: number; request_interval_seconds: number;
  input_text_limit: number; output_tokens: OutputTokens;
};
type Profiles = { active_id: string; profiles: Profile[] };
type Draft = Omit<Profile, "request_interval_seconds" | "input_text_limit" | "output_tokens"> & {
  request_interval_seconds: number | ""; input_text_limit: number | "";
  output_tokens: Record<keyof OutputTokens, number | "">;
};
const empty: Draft = {
  id: "", name: "", base_url: "", model: "", has_token: false,
  stream: true, json_mode: false, section_concurrency: 3, request_interval_seconds: 3,
  input_text_limit: 25000,
  output_tokens: { outline: 32768, extract: 32768, chapters: 32768, overview: 16384, issue_quotes: 32768, connection_test: 1024 },
};
const root = "/settings/model/profiles";

// 管理多组 API 的编辑草稿、密钥输入和可取消请求，不把密钥存入浏览器。
export function useAPISettings(onSaved: (close: boolean) => void) {
  const [saved, setSaved] = useState<Profiles | null>(null);
  const [editing, setEditing] = useState<Draft | null>(null);
  const [token, setToken] = useState("");
  const [models, setModels] = useState<string[]>([]);
  const [manualModel, setManualModel] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [failed, setFailed] = useState(false);
  const lifetime = useRef<AbortController | null>(null);
  const pending = useRef(false);
  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    void api<Profiles>(root, { signal: controller.signal }).then(result => {
      if (controller.signal.aborted) return;
      setSaved(result);
      setEditing(result.profiles.find(p => p.id === result.active_id)!);
    }).catch(error => {
      if (controller.signal.aborted || isAbortError(error)) return;
      setFailed(true);
      if (!(error instanceof ReportedError)) throw error;
    });
    return () => controller.abort();
  }, []);

  function select(profile: Draft) {
    setEditing({ ...profile }); setToken(""); setModels([]); setManualModel(false); setMessage("");
  }
  function update(value: Partial<Draft>) {
    setEditing(previous => previous && { ...previous, ...value });
    if (value.base_url !== undefined) setModels([]);
  }
  function changeToken(value: string) { setToken(value); setModels([]); }
  function request<T>(signal: AbortSignal, path = "", method = "GET", body?: object) {
    return api<T>(root + path, { method, signal, headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined });
  }
  // 统一阻止重复操作，表单关闭后取消请求并隔离迟到的完成回调。
  async function run<T>(action: (signal: AbortSignal) => Promise<T>, complete: (value: T) => void) {
    const signal = lifetime.current?.signal;
    if (!signal || signal.aborted || pending.current) return;
    pending.current = true; setBusy(true); setMessage("");
    try {
      const result = await action(signal);
      if (!signal.aborted) complete(result);
    } catch (error) {
      if (!signal.aborted && !isAbortError(error) && !(error instanceof ReportedError)) throw error;
    } finally {
      pending.current = false;
      if (!signal.aborted) setBusy(false);
    }
  }
  // 仅提交公开允许的配置字段，留空密钥与显式清除使用不同标志。
  function values(clear = false) {
    const p = editing!;
    return { name: p.name, base_url: p.base_url, model: p.model, stream: p.stream, json_mode: p.json_mode,
      section_concurrency: p.section_concurrency, request_interval_seconds: p.request_interval_seconds,
      input_text_limit: p.input_text_limit, output_tokens: p.output_tokens,
      token: clear ? "" : token, clear_token: clear };
  }
  // 以保存返回值更新连接列表和表单，再通知新任务可使用已确认配置。
  function save(clear = false) {
    return run(signal => request<Profiles>(signal, editing!.id ? `/${editing!.id}` : "",
      editing!.id ? "PUT" : "POST", values(clear)), result => {
      const selected = editing!.id ? result.profiles.find(p => p.id === editing!.id)! : result.profiles.at(-1)!;
      setSaved(result); select(selected); onSaved(false);
      setMessage(clear ? "已删除这组配置的密钥。" : selected.id === result.active_id
        ? "配置已保存，后续新任务使用此连接；运行中的任务保持原设置。"
        : "配置已保存，点击“启用此配置”供新任务使用。");
      notifySuccess(clear ? "API 密钥已删除。" : "API 配置保存成功。");
    });
  }
  const query = editing?.id ? `?profile_id=${encodeURIComponent(editing.id)}` : "";
  // 使用当前表单读取可选模型，不先保存，也不把列表结果视为连接测试。
  function readModels() {
    return run(signal => request<{ models: string[] }>(signal, "/options" + query, "POST", values()), result => {
      setModels(result.models); setManualModel(false);
      setMessage(`已读取 ${result.models.length} 个模型，请在模型 ID 中选择，也可切换为手动输入。`);
    });
  }
  function test() {
    return run(signal => request<{ elapsed_seconds: number }>(signal, "/test" + query, "POST", values()),
      result => notifySuccess(`连接成功，耗时 ${result.elapsed_seconds} 秒。`));
  }
  function activate() {
    return run(signal => request<Profiles>(signal, `/${editing!.id}/activate`, "POST"), result => {
      setSaved(result); select(result.profiles.find(p => p.id === editing!.id)!); onSaved(false);
      setMessage("已启用保存的配置，新任务将使用此连接。");
    });
  }
  function remove() {
    return run(signal => request<Profiles>(signal, `/${editing!.id}`, "DELETE"), result => {
      setSaved(result); select(result.profiles.find(p => p.id === result.active_id)!); onSaved(false);
      setMessage("已删除配置。");
    });
  }
  const original = saved?.profiles.find(p => p.id === editing?.id);
  const hasCredential = !!token || !!(original?.has_token && original.base_url === editing?.base_url.replace(/\/+$/, ""));
  return { saved, editing, update, token, changeToken, models, manualModel, setManualModel, busy, message, failed,
    select, create: () => select({ ...empty }), save, readModels, test, activate, remove, original, hasCredential };
}
