import { useScopedRead } from "../shared/useScopedRead";

export type ReadState = { loading: boolean; error: string | null; retry: () => void };

// 以任务身份和正文修订号读取笔记，沿用共用读取的缓存保留与重试规则。
export function useNoteResult<T>({ identity, path, revision, enabled, onError }: {
  identity: string; path: string; revision: number; enabled: boolean;
  onError: (error: unknown) => void;
}) {
  const { data, ...reading } = useScopedRead<T>({ identity, path, refreshKey: revision, enabled,
    errorMessage: "笔记读取失败，请重新读取。", onError });
  return { result: data, ...reading };
}
