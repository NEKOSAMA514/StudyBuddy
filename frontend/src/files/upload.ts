import { apiUrl, decodeJSONResponse, requestConnectionError } from "../api";
import type { LibraryFile } from "../types";

// 用 XMLHttpRequest 提供上传进度，文档延后提取，响应沿用统一错误处理。
export function uploadFile(
  file: File,
  onProgress: (percent: number) => void,
  document = false,
): Promise<LibraryFile> {
  const body = new FormData();
  body.append("file", file);
  if (document) body.append("defer_extraction", "true");
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const endpoint = document ? "/documents" : "/videos";
    xhr.open("POST", apiUrl(endpoint));
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable)
        onProgress(Math.round((100 * e.loaded) / e.total));
    };
    xhr.onerror = () => { void requestConnectionError(new Error("上传连接中断，请重新上传。"), apiUrl(endpoint), "POST", undefined, "caller").catch(reject); };
    xhr.onload = () => {
      void decodeJSONResponse<LibraryFile>(xhr.responseText, xhr.status, apiUrl(endpoint), "POST", undefined, "caller").then(resolve, reject);
    };
    xhr.send(body);
  });
}
