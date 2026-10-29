import type { Navigate } from "./navigation";

// 展示资料分析与笔记中心入口，具体读取和任务操作交给目标页面。
export function Home({ navigate }: { navigate: Navigate }) {
  return (
    <section className="home-screen">
      <div className="home-mark">
        S<span>✦</span>
      </div>
      <h1>今天，想从哪里开始？</h1>
      <p>查找已有笔记，或把资料整理成清晰的笔记。</p>
      <div className="home-options">
        <button onClick={() => navigate("notes")}>
          <span className="option-icon">◈</span>
          <strong>笔记中心</strong>
          <small>搜索笔记，集中回看重点</small>
          <span className="option-arrow">↗</span>
        </button>
        <button onClick={() => navigate("summary")}>
          <span className="option-icon">▤</span>
          <strong>开始总结？</strong>
          <small>上传视频，整理有依据的笔记</small>
          <span className="option-arrow">↗</span>
        </button>
      </div>
      <div className="home-footnote">
        <span>◌ 本地处理视频</span>
        <span>↗ 时间戳回看</span>
        <span>✓ 原文依据</span>
      </div>
    </section>
  );
}
