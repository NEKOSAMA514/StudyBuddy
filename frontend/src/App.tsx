import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import type { Health, TaskReceipt } from "./types";
import { usePageRoute, noteCenterLink } from "./navigation";
import { useNotifications } from "./useNotifications";
import { useFiles } from "./files/useFiles";
import { useDownloadAnalysis } from "./analysis/useDownloadAnalysis";
import { VideoDetail } from "./analysis/VideoDetail";
import { FileLibrary } from "./FileLibrary";
import { Sidebar } from "./Sidebar";
import { Home } from "./Home";
import { NoteSearch } from "./notes/NoteSearch";
import { ModelSettingsDialog } from "./settings/ModelSettings";
import { DocumentDetail } from "./documents/DocumentDetail";

// 装配导航、共享文件列表和各详情页，设置覆盖层保留底下页面状态。
export function App() {
  const [theme, setTheme] = useState(() =>
    document.documentElement.dataset.theme === "dark" ? "dark" : "light",
  );
  function toggleTheme() {
    const next = theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    setTheme(next);
    try {
      localStorage.setItem("studybuddy-theme", next);
    } catch {
      // 浏览器禁止保存时，本页仍可切换主题。
    }
  }
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => {
    try {
      return localStorage.getItem("studybuddy-sidebar-collapsed") === "true";
    } catch {
      return false;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(
        "studybuddy-sidebar-collapsed",
        String(sidebarCollapsed),
      );
    } catch {
      // 存储不可用时仍允许使用侧栏与主题切换。
    }
  }, [sidebarCollapsed]);
  const navigation = usePageRoute();
  const { route, settingsOpen, closeSettings } = navigation;
  const { onError, notification } = useNotifications();
  const [health, setHealth] = useState<Health | null>(null);
  const [settingsNotice, setSettingsNotice] = useState("");
  // 这里只组合全局禁用范围；具体操作与防重复提交仍归各功能模块。
  const [detailBusy, setDetailBusy] = useState(false);
  const [downloadBusy, setDownloadBusy] = useState(false);
  const activeFileId = route.mode === "summary" ? route.fileId : undefined;
  // 下载建任务后的回执只在应用层短期交接，详情接管或离开目标页后清除。
  const [handoff, setHandoff] = useState<{ id: string; receipt: Pick<TaskReceipt, "task_id"> } | null>(null);
  const taken = useCallback(() => setHandoff(null), []);
  useEffect(() => {
    if (handoff && activeFileId !== handoff.id) setHandoff(null);
  }, [activeFileId, handoff]);
  function navigate(
    mode: Parameters<typeof navigation.navigate>[0],
    fileId?: string,
  ) {
    setSidebarOpen(false);
    navigation.navigate(mode, fileId);
  }
  const files = useFiles({
    health,
    activeFileId,
    blocked: detailBusy || downloadBusy,
    onError,
    onUploaded: (file, receipt) => {
      if (receipt) setHandoff({ id: file.id, receipt });
      navigate("summary", file.id);
    },
    onDeleted: (id) => {
      if (activeFileId === id) navigate("summary");
    },
  });
  const download = useDownloadAnalysis({
    blocked: files.busy || detailBusy,
    refresh: files.refresh,
    onError,
    onOpen: (video, receipt) => {
      files.remember(video);
      setHandoff({ id: video.id, receipt });
      navigate("summary", video.id);
    },
    onBusyChange: setDownloadBusy,
    navigationKey: `${route.mode}/${route.fileId || ""}/${settingsOpen}`,
  });
  const busy = files.busy || detailBusy || downloadBusy;
  const selectedFile = files.library.find((item) => item.id === activeFileId);
  useEffect(() => {
    onError("");
  }, [activeFileId, onError]);
  useEffect(() => {
    const controller = new AbortController();
    api<Health>("/health", { errorPolicy: "caller", signal: controller.signal })
      .then((info) => {
        if (!controller.signal.aborted) setHealth(info);
      })
      .catch((error) => {
        if (!controller.signal.aborted) onError(error);
      });
    return () => controller.abort();
  }, [onError]);
  // 保存成功后重新读取健康摘要，是否关闭覆盖层由具体表单决定。
  function settingsSaved(close: boolean) {
    void api<Health>("/health", { errorPolicy: "caller" }).then(setHealth).catch(onError);
    if (close) {
      setSidebarOpen(false);
      closeSettings();
      setSettingsNotice(
        "默认设置已保存。开始处理前可以选择本次模型；重试保留原任务的模型选择。",
      );
    }
  }
  return (
    <div
      className={`shell ${sidebarOpen ? "sidebar-open" : ""} ${sidebarCollapsed ? "sidebar-collapsed" : ""}`}
    >
      {sidebarOpen && (
        <button
          className="sidebar-backdrop"
          aria-label="收起侧边栏"
          onClick={() => setSidebarOpen(false)}
        />
      )}
      <Sidebar
        mode={route.mode}
        library={files.library}
        activeFileId={activeFileId}
        busy={busy}
        navigate={navigate}
        onAction={(file, action) => void files.act(file, action)}
        onCollapse={() => {
          setSidebarCollapsed(true);
          setSidebarOpen(false);
        }}
      />
      <main>
        <header className="topbar">
          <div className="topbar-left">
            <button
              className="sidebar-toggle"
              aria-label={sidebarOpen ? "折叠侧边栏" : "展开侧边栏"}
              aria-expanded={sidebarOpen}
              aria-controls="main-sidebar"
              onClick={() => setSidebarOpen((open) => !open)}
            >
              ☰
            </button>
            {sidebarCollapsed && (
              <button
                className="desktop-sidebar-toggle"
                aria-label="展开侧边栏"
                title="展开侧边栏"
                aria-expanded="false"
                aria-controls="main-sidebar"
                onClick={() => setSidebarCollapsed(false)}
              >
                ☰
              </button>
            )}
          </div>
          <div className="topbar-theme">
            <span className="theme-label">明暗主题切换</span>

            <button
              type="button"
              className="theme-toggle"
              onClick={toggleTheme}
              aria-label={
                theme === "light" ? "切换到暗色模式" : "切换到亮色模式"
              }
              title={theme === "light" ? "切换到暗色模式" : "切换到亮色模式"}
            >
              <svg viewBox="0 0 24 24" aria-hidden="true">
                {theme === "light" ? (
                  <path d="M20.5 13.1A8.5 8.5 0 0 1 10.9 3.5a8.5 8.5 0 1 0 9.6 9.6Z" />
                ) : (
                  <>
                    <circle cx="12" cy="12" r="4" />
                    <path d="M12 2v2m0 16v2M2 12h2m16 0h2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
                  </>
                )}
              </svg>
            </button>
          </div>
        </header>
        {settingsNotice && (
          <p role="status" className="notice">
            {settingsNotice}
          </p>
        )}
        {route.mode === "home" ? (
          <Home navigate={navigate} />
        ) : route.mode === "notes" ? (
          <NoteSearch query={route.query || ""} page={route.page || 1} view={route.view || "search"} />
        ) : !route.fileId ? (
          <FileLibrary
            files={files.library}
            busy={busy || !health}
            loading={files.loading}
            loadError={files.loadError}
            retryRead={() => void files.refresh().catch(onError)}
            uploadPercent={files.uploadPercent}
            maxUploadBytes={health?.max_upload_bytes || 2 * 1024 ** 3}
            maxDocumentBytes={health?.max_document_bytes || 100 * 1024 ** 2}
            extensions={[...(health?.supported_video_extensions || []), ...(health?.supported_document_extensions || [])]}
            onUpload={(items) => void files.upload(items)}
            onGenerateNotes={download.open}
            onOpen={(id) => navigate("summary", id)}
            onAction={(id, action) => {
              const item = files.library.find((file) => file.id === id);
              if (item) void files.act(item, action);
            }}
          />
        ) : !selectedFile ? (
          <section className="notice">
              {files.loadError ? <><p role="alert">文件列表读取失败，请重新读取。</p><button className="secondary" disabled={files.loading} onClick={() => void files.refresh().catch(onError)}>重新读取</button></> :
              <p>{files.resolvingImport ? files.extractionReconnecting ? "正在重新连接，恢复文档提取记录…" : "正在恢复文档提取记录…" : files.loaded ? "文件不存在或已被删除。" : "正在读取文件…"}</p>}
            {route.target && <a href={noteCenterLink(route.target.returnTo)}>← 返回笔记中心</a>}
            <button className="text-button" onClick={() => navigate("summary")}>
              返回
            </button>
          </section>
        ) : selectedFile.kind === "document" ? (
            <>
          <DocumentDetail key={selectedFile.id} document={selectedFile} target={route.target} onBack={() => navigate("summary")} onError={onError}
            receipt={handoff?.id === selectedFile.id ? handoff.receipt : undefined} onTaken={taken}
            processDocument={files.processDocument} processing={files.busy} processingStage={files.importStage} processingError={files.importError}
              extractionReconnecting={files.extractionReconnecting}
            onTaskChanged={files.taskChanged} refresh={files.refresh} onBusyChange={setDetailBusy} settingsOpen={settingsOpen} />
          </>
        ) : (
          <VideoDetail
            key={selectedFile.id}
            video={selectedFile}
            receipt={handoff?.id === selectedFile.id ? handoff.receipt : undefined}
            onTaken={taken}
            target={route.target}
            health={health}
            blocked={files.busy || downloadBusy}
            uploadPercent={files.uploadPercent}
            onUpload={files.upload}
            onBack={() => navigate("summary")}
            onTaskChanged={files.taskChanged}
            refresh={files.refresh}
            onError={onError}
            onBusyChange={setDetailBusy}
            settingsOpen={settingsOpen}
          />
        )}
      </main>
      <button type="button" className="back-to-top" aria-label="回到顶端" title="回到顶端"
        onClick={() => window.scrollTo({ top: 0, behavior: "smooth" })}>
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 14 6-6 6 6" /></svg>
      </button>
      {files.dialog}
      {download.dialog}
      {settingsOpen && (
        <ModelSettingsDialog
          onClose={() => {
            setSidebarOpen(false);
            closeSettings();
          }}
          onSaved={settingsSaved}
        />
      )}
      {notification}
    </div>
  );
}
