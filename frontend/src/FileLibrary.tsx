import { taskStatusLabels } from "./shared/taskStatus";
import { useRef, useState } from "react";
import { FileMenu, type FileAction } from "./files/FileActions";
import { VideoDownload } from "./VideoDownload";

import type { LibraryFile } from "./types";

// 展示上传入口、排序后的文件和读取恢复状态，写操作由共用文件 Hook 执行。
export function FileLibrary({ files, busy, loading, loadError, retryRead, uploadPercent, maxUploadBytes, maxDocumentBytes, extensions,
  onUpload, onOpen, onAction, onGenerateNotes }: {
  files: LibraryFile[];
  busy: boolean;
  loading: boolean;
  loadError: boolean;
  retryRead: () => void;
  uploadPercent: number | null;
  maxUploadBytes: number;
  maxDocumentBytes: number;
  extensions: string[];
  onUpload: (files: FileList | null) => void;
  onOpen: (id: string) => void;
  onAction: (id: string, action: FileAction) => void;
  onGenerateNotes: (id: string, filename: string) => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  const dragDepth = useRef(0);
  const [dragging, setDragging] = useState(false);
  return (
    <div className="file-library">
      <div className="heading">
        <div><h1>总结分析</h1><p>上传资料，也可以继续查看已有文件。</p></div>
      </div>
      <VideoDownload onGenerateNotes={onGenerateNotes} disabled={busy} />
      <section className={`file-upload ${dragging ? "dragging" : ""}`} aria-label="文件上传"
        onDragEnter={event => {
          if (busy || !event.dataTransfer.types.includes("Files")) return;
          event.preventDefault();
          dragDepth.current += 1;
          setDragging(true);
        }}
        onDragOver={event => {
          if (!event.dataTransfer.types.includes("Files")) return;
          event.preventDefault();
          event.dataTransfer.dropEffect = busy ? "none" : "copy";
        }}
        onDragLeave={event => {
          event.preventDefault();
          dragDepth.current = Math.max(0, dragDepth.current - 1);
          if (!dragDepth.current) setDragging(false);
        }}
        onDrop={event => {
          event.preventDefault();
          dragDepth.current = 0;
          setDragging(false);
          if (!busy) onUpload(event.dataTransfer.files);
        }}>
        <input ref={input} type="file" accept={extensions.map(ext => `.${ext}`).join(",")} aria-label="选择本地文件" hidden disabled={busy}
          onChange={event => { onUpload(event.target.files); event.target.value = ""; }} />
        <button className="file-upload-button" type="button" disabled={busy} onClick={() => input.current?.click()}>
          <span className="file-upload-plus" aria-hidden="true">＋</span>
          <strong>{uploadPercent !== null ? `正在上传 ${uploadPercent}%` : "点击或拖拽文件到这里"}</strong>
          <span>支持视频、PDF、DOCX、PPTX、XLSX、CSV、TXT、Markdown</span>
          <span>视频最大 {Number((maxUploadBytes / 1024 ** 3).toFixed(2))} GB · 最长 2 小时；文档最大 {Math.round(maxDocumentBytes / 1024 ** 2)} MB</span>
        </button>
        {uploadPercent !== null && <progress aria-label="文件上传进度" max={100} value={uploadPercent} />}
      </section>
      <section className="panel file-list" aria-label="文件列表">
        <div className="panel-header"><h2>文件列表</h2><span>{files.length} 个文件</span></div>
        {loadError && <p className="notice" role="alert">文件列表读取失败，请重新读取。<button className="secondary" disabled={loading} onClick={retryRead}>重新读取</button></p>}
        <div className="file-table-scroll">
          <table>
            <thead><tr><th scope="col">名称</th><th scope="col">类型</th><th scope="col">大小</th><th scope="col">上传时间</th><th scope="col">状态</th><th scope="col">操作</th></tr></thead>
            <tbody>{files.map(file => (
              <tr key={file.id}>
                <td><button className="file-name" disabled={busy} onClick={() => onOpen(file.id)}>
                  {file.pinned && <span className="file-pin">置顶</span>}{file.filename}
                </button></td>
                <td>{file.kind === "document" ? `文档 · ${file.format.toUpperCase()}` : `视频 · ${file.media_info?.container?.toUpperCase() || "MP4"}`}</td>
                <td>{file.size_bytes >= 1048576 ? `${(file.size_bytes / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(file.size_bytes / 1024))} KB`}</td>
                <td>{new Date(file.created_at).toLocaleString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false })}</td>
                <td><span className={`status-pill ${file.latest_task?.status || ""}`}>{(file.latest_task && taskStatusLabels[file.latest_task.status]) || (file.kind === "document" ? file.extracted === false ? "待处理" : "已提取" : "未处理")}</span></td>
                <td><FileMenu file={file} disabled={busy} onAction={action => onAction(file.id, action)} /></td>
              </tr>
            ))}</tbody>
          </table>
        </div>
        {!files.length && !loadError && <p className="file-list-empty">{loading ? "正在读取文件…" : "还没有文件，上传第一份资料开始整理。"}</p>}
      </section>
    </div>
  );
}
