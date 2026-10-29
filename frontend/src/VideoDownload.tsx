import { notifyError } from "./notifications";
import { reportClientError } from "./api";
import { BilibiliLogin } from "./BilibiliLogin";
import { useVideoDownload, type QualityOption } from "./downloads/useVideoDownload";

const stages: Record<string, string> = { completed: "下载完成", failed: "下载失败", interrupted: "下载已中断" };

function qualityLabel(height: number, options?: QualityOption[]) {
  const option = options?.find(item => item.height === height);
  if (!option) return `${height}P · 格式未知 · 大小未知`;
  const bytes = option.filesize;
  const size = bytes === null ? "大小未知" : `${option.approximate ? "约 " : ""}${bytes >= 1024 ** 3 ? (bytes / 1024 ** 3).toFixed(2) + " GB" : bytes >= 1024 ** 2 ? (bytes / 1024 ** 2).toFixed(1) + " MB" : Math.ceil(bytes / 1024) + " KB"}`;
  return `${height}P · ${option.ext?.toUpperCase() || "格式未知"} · ${size}`;
}

// 展示链接、分 P、画质和下载状态，业务流程统一由下载 Hook 管理。
export function VideoDownload({ onGenerateNotes, disabled }: {
  onGenerateNotes: (id: string, filename: string) => void;
  disabled: boolean;
}) {
  const { input, download, login } = useVideoDownload();
  const { url, metadata, quality, selectedPart, metadataReady, pendingPart, displayedDuration, busy, bilibiliLink,
    parse, selectPart, selectQuality, changeUrl } = input;
  const { task, running, localPath, openingFolder, pollingError, start, openFolder, retry: retryDownload } = download;
  const { session: bilibiliSession, intent: loginIntent, continueParsing, open: openLogin, close: closeLogin, complete: completeLogin } = login;

  return <section className="panel video-download" aria-label="视频链接下载">
    <div className="panel-header"><h2>从视频链接下载</h2><span>B 站、YouTube 等</span></div>
    <form className="video-link-form" onSubmit={event => { event.preventDefault(); void parse(); }}>
      <label htmlFor="video-link">视频链接</label>
      <div className="video-link-controls">
        <input id="video-link" type="text" required placeholder="粘贴视频链接、B站分享文案或 BV 号" value={url} disabled={busy || running}
          onChange={event => changeUrl(event.target.value)} />
        <button type="submit" className="secondary" disabled={busy || running || !url.trim()}>{busy && !task ? "正在解析…" : "解析链接"}</button>
      </div>
    </form>
    {bilibiliLink && (bilibiliSession?.state === "logged_out" || bilibiliSession?.state === "expired") && <div className="bilibili-session" role="status">
      <span>扫码登录B站，使用AI字幕无需本地语音转写</span>
      <button type="button" disabled={busy || running} onClick={openLogin}>扫码登录</button>
    </div>}
    {loginIntent && <BilibiliLogin immediate={loginIntent.immediate} canContinue={!!loginIntent.url}
      session={bilibiliSession} onClose={closeLogin} onContinue={continueParsing}
      onSuccess={completeLogin} />}
    {metadata && <div className="video-link-preview">
      {metadata.thumbnail && <img src={metadata.thumbnail} alt="视频封面" referrerPolicy="no-referrer" onError={event => { event.currentTarget.hidden = true; }} />}
      <div><h3>{pendingPart ? `P${pendingPart.page} ${pendingPart.title}` : metadata.title}</h3><p>时长：{displayedDuration == null ? "未知" : `${Math.floor(displayedDuration / 60)}:${String(Math.floor(displayedDuration % 60)).padStart(2, "0")}`}</p>
        {metadata.parts && metadata.parts.length > 1 && <div className="video-quality-controls">
          <label htmlFor="video-part">视频分 P</label>
          <select id="video-part" value={selectedPart} disabled={running || (busy && metadataReady)} onChange={event => selectPart(Number(event.target.value))}>
            {metadata.parts.map(part => <option key={part.page} value={part.page}>P{part.page} · {part.title} · {Math.floor(part.duration / 60)}:{String(Math.floor(part.duration % 60)).padStart(2, "0")}</option>)}
          </select>
        </div>}
        <p> </p>
        {busy && <p role="status">正在读取视频信息…</p>}
        <div className="video-quality-controls"><label htmlFor="video-quality">下载画质</label>
          <select id="video-quality" value={quality} disabled={busy || running || !metadataReady} onChange={event => selectQuality(Number(event.target.value))}>
            {quality === "" && <option value="" disabled>请选择画质</option>}
            {metadata.qualities.map(height => <option key={height} value={height}>{qualityLabel(height, metadata.quality_options)}</option>)}
          </select>
          <button type="button" className="primary" disabled={busy || running || !metadataReady || quality === ""} onClick={() => void start()}>{running ? "下载中…" : "下载视频"}</button>
        </div>
        {metadata.default_quality === null && <p>请选择画质。</p>}
      </div>
    </div>}
    {task && <div className="video-download-status" role="status">
      {!running && <strong>{stages[task.stage] || task.stage}</strong>}
      {running && <>
        <progress aria-label="下载进度" max={100} value={task.progress ?? undefined} />
        {task.progress !== null && <small>{task.progress.toFixed(1)}%</small>}
      </>}
      {task.status === "succeeded" && <div className="video-download-actions">
        <button type="button" className="primary" disabled={disabled} onClick={() => onGenerateNotes(task.id, task.metadata?.title || "下载的视频")}>开始生成笔记</button>
        <button type="button" className="secondary" disabled={!localPath} onClick={() => void navigator.clipboard.writeText(localPath!).catch(error => void reportClientError(error, "下载路径复制失败，请重试。").then(notifyError))}>复制下载路径</button>
        <button type="button" className="secondary" disabled={openingFolder} onClick={() => void openFolder()}>{openingFolder ? "正在打开…" : "打开文件所在位置"}</button>
      </div>}
      {task.error && <small>下载未完成，可重新下载。详细信息见项目日志{task.error.error_id ? `，错误编号 ${task.error.error_id}` : ""}。</small>}
    </div>}
    {pollingError && (
      <div className="video-download-retry">
        <button
          type="button"
          className="secondary"
          onClick={retryDownload}
        >
          重新读取下载状态
        </button>
      </div>
    )}  </section>;
}
