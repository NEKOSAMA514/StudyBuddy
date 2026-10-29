import { taskStatusLabels } from "./shared/taskStatus";
import { FileMenu, type FileAction } from "./files/FileActions";
import type { LibraryFile } from "./types";
import type { Mode, Navigate } from "./navigation";
import { clock } from "./util/time";

// 共用文件列表展示侧栏导航，并把文件操作和页面切换交回应用层。
export function Sidebar({
  mode,
  library,
  activeFileId,
  busy,
  navigate,
  onAction,
  onCollapse,
}: {
  mode: Mode;
  library: LibraryFile[];
  activeFileId?: string;
  busy: boolean;
  navigate: Navigate;
  onAction: (file: LibraryFile, action: FileAction) => void;
  onCollapse: () => void;
}) {
  return (
    <aside className="sidebar" id="main-sidebar">
      <div className="sidebar-header">
        <a className="brand" href="/">
          StudyBuddy
        </a>
        <button
          className="desktop-sidebar-toggle"
          aria-label="折叠侧边栏"
          title="折叠侧边栏"
          aria-expanded="true"
          aria-controls="main-sidebar"
          onClick={() => {
            onCollapse();
          }}
        >
          ☰
        </button>
      </div>
      <a href="#" className={`mode-link ${mode === "home" ? "active" : ""}`}>
        ＋ 新的开始
      </a>
      <div className="nav-label">笔记中心</div>
      <button
        className={`mode-link ${mode === "notes" ? "active" : ""}`}
        aria-label="笔记中心"
        onClick={() => navigate("notes")}
      >
        <span>◈</span> 笔记中心
      </button>
      <div className="nav-label">总结分析模式</div>
      <button
        className={`mode-link ${mode === "summary" ? "active" : ""}`}
        aria-label="开始总结"
        onClick={() => navigate("summary")}
      >
        <span>▤</span> 开始总结
      </button>
      <div className="history-label">
        文件 <span>{library.length}</span>
      </div>
      <nav aria-label="已上传的文件">
        {library.map((file) => {
          const status = file.latest_task?.status;
          return (
            <div
              className={`library-row ${activeFileId === file.id ? "selected" : ""}`}
              key={file.id}
            >
              <button
                disabled={busy}
                className={`library-item ${activeFileId === file.id ? "selected" : ""}`}
                onClick={() => {
                  navigate("summary", file.id);
                }}
              >
                <span
                  className={`file-glyph ${status || ""}`}
                  aria-hidden="true"
                >
                  {file.kind === "document" ? "▤" : "▶"}
                </span>
                <span className="library-name">
                  {file.filename}
                  <small>
                    {file.pinned ? "置顶 · " : ""}
                    <>{file.kind === "document" ? file.format.toUpperCase() : clock(file.duration)} · {status === "succeeded" ? "已总结" : status ? taskStatusLabels[status] : file.kind === "document" ? file.extracted === false ? "待处理" : "已提取" : "待分析"}</>
                  </small>
                </span>
              </button>
              <FileMenu
                file={file}
                disabled={busy}
                onAction={(action) => onAction(file, action)}
              />
            </div>
          );
        })}
      </nav>
      {!library.length && (
        <p className="library-empty">
          上传第一个文件，
          <br />
          在这里继续你的学习。
        </p>
      )}
      <div className="sidebar-bottom">
        <a className="settings-link" href="#settings">
          ⚙ 设置
        </a>
        <span className="dot" />
        本机工作台
        <p>
          视频与截图保存在本地
          <br />
          只有识别文本用于云端总结
        </p>
      </div>
    </aside>
  );
}
