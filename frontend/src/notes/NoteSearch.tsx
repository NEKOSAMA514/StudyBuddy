import { noteCenterLink, noteLocationLink } from "../navigation";
import { Fragment, useEffect, useState } from "react";
import { api } from "../api";

type Hit = {
  kind: "video" | "document"; file_id: string; filename: string; filename_highlights: number[][]; task_id: string;
  note_title: string; area: "overview" | "chapter"; chapter_id: string | null;
  title: string; title_highlights: number[][]; snippet: string; snippet_highlights: number[][];
};
type SearchResult = { items: Hit[]; total: number; page: number; page_size: number };

// 按后端返回的字符区间插入高亮节点，原始文本继续作为文本渲染。
function Highlight({ text, ranges }: { text: string; ranges: number[][] }) {
  let cursor = 0;
  const parts = ranges.map(([start, end]) => {
    const before = text.slice(cursor, start);
    cursor = end;
    return <Fragment key={start}>{before}<mark>{text.slice(start, end)}</mark></Fragment>;
  });
  return <>{parts}{text.slice(cursor)}</>;
}

// 章节标题优先，摘要次之，文件和版本弱化；复用已有面板、控件与主题。
export function NoteSearch({ query, page, view = "search" }: { query: string; page: number; view?: "search" | "marks" }) {
  return <section className="note-search" aria-label="笔记中心">
    <div className="heading"><h1>笔记中心</h1></div>
    <nav className="note-center-views" aria-label="笔记中心内容">
      <a className="secondary" aria-current={view === "search" ? "page" : undefined}
        href={noteCenterLink({ query, page: 1, view: "search" })}>搜索笔记</a>
      <a className="secondary" aria-current={view === "marks" ? "page" : undefined}
        href={noteCenterLink({ query, page: 1, view: "marks" })}>我的重点</a>
    </nav>
    {view === "marks" ? <MarkedNotes page={page} query={query} /> : <SearchResults query={query} page={page} />}
  </section>;
}

// 按查询和页码取消旧搜索，并将命中按资料分组后构造精确定位链接。
function SearchResults({ query, page }: { query: string; page: number }) {
  const [input, setInput] = useState(query);
  const [attempt, setAttempt] = useState(0);
  const [response, setResponse] = useState<{ key: string; result?: SearchResult; error?: string } | null>(null);
  const key = JSON.stringify([query, page, attempt]);
  useEffect(() => { setInput(query); }, [query, page]);
  useEffect(() => {
    if (!query.trim()) return;
    const controller = new AbortController();
    api<SearchResult>(`/notes/search?${new URLSearchParams({ q: query, page: String(page) })}`, { signal: controller.signal })
      .then(result => { if (!controller.signal.aborted) setResponse({ key, result }); })
      .catch(error => { if (!controller.signal.aborted) setResponse({ key, error: error instanceof Error ? error.message : "搜索失败，请重试。" }); });
    return () => controller.abort();
  }, [query, page, key]);
  const current = response?.key === key ? response : null;
  const result = current?.result;
  const groups = new Map<string, Hit[]>();
  result?.items.forEach(hit => {
    const id = `${hit.kind}/${hit.file_id}`;
    groups.set(id, [...(groups.get(id) || []), hit]);
  });
  function search(nextPage = 1, value = query) {
    location.hash = noteCenterLink({ query: value.trim(), page: nextPage, view: "search" });
  }
  return <>
    <p className="muted">搜索文件名和最新完成的笔记。多个关键词用空格分隔。</p>
    <form className="note-search-form" role="search" onSubmit={event => {
      event.preventDefault();
      if (input.trim() === query && page === 1) setAttempt(value => value + 1);
      else search(1, input);
    }}>
      <label htmlFor="note-search-input">搜索笔记</label>
      <div><input id="note-search-input" type="search" value={input} maxLength={200}
        placeholder="输入概念或关键词" onChange={event => setInput(event.target.value)} />
        <button className="primary">搜索</button></div>
    </form>
    {!query.trim() ? <p role="status" className="note-search-state">输入关键词，查找视频和文档笔记中的相关内容。</p>
      : !current ? <p role="status" className="note-search-state">正在搜索…</p>
      : current.error ? <div role="status" className="note-search-state"><p>{current.error}</p>
        <button className="secondary" onClick={() => setAttempt(value => value + 1)}>重新搜索</button></div>
      : result && <>
        <p role="status">{result.total ? `找到 ${result.total} 处相关内容` : "没有找到相关内容，试试更短的关键词。"}</p>
        {[...groups].map(([id, hits]) => <section className="panel note-search-group" key={id}>
          <header><span className="eyebrow">{hits[0].kind === "video" ? "视频" : "文档"}</span>
            <h2><Highlight text={hits[0].filename} ranges={hits[0].filename_highlights} /></h2></header>
          {hits.map(hit => {
            return <article className="note-search-hit" key={`${hit.task_id}/${hit.chapter_id || "overview"}`}>
              <span className="muted">{hit.area === "overview" ? "文件名、标题与概览" : "章节"}</span>
              <h3><a href={noteLocationLink(hit.file_id, { taskId: hit.task_id, chapterId: hit.chapter_id || undefined, returnTo: { query, page, view: "search" } })}>
                <Highlight text={hit.title} ranges={hit.title_highlights} /></a></h3>
              <p><Highlight text={hit.snippet} ranges={hit.snippet_highlights} /></p>
            </article>;
          })}
        </section>)}
        {result.total > result.page_size && <nav className="note-search-pagination" aria-label="搜索结果分页">
          <button className="secondary" disabled={page <= 1} onClick={() => search(page - 1)}>上一页</button>
          <span>第 {page} / {Math.ceil(result.total / result.page_size)} 页</span>
          <button className="secondary" disabled={page * result.page_size >= result.total} onClick={() => search(page + 1)}>下一页</button>
        </nav>}
      </>}
  </>;
}

type MarkedItem = {
  kind: "video" | "document"; file_id: string; filename: string; task_id: string; note_title: string;
  version: number; chapter_id: string; chapter_title: string; heading_index: number; title: string; snippet: string;
};
type MarkedResult = { items: MarkedItem[]; total: number; page: number; page_size: number };

// 读取当前页仍有效的版本重点，并保留返回重点列表的位置。
function MarkedNotes({ page, query }: { page: number; query: string }) {
  const [attempt, setAttempt] = useState(0);
  const key = JSON.stringify([page, attempt]);
  const [response, setResponse] = useState<{ key: string; result?: MarkedResult; error?: string } | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    api<MarkedResult>(`/note-marks?page=${page}`, { signal: controller.signal })
      .then(result => { if (!controller.signal.aborted) setResponse({ key, result }); })
      .catch(error => { if (!controller.signal.aborted) setResponse({ key, error: error instanceof Error ? error.message : "重点读取失败。" }); });
    return () => controller.abort();
  }, [page, key]);
  const current = response?.key === key ? response : null;
  const result = current?.result;
  const groups = new Map<string, MarkedItem[]>();
  result?.items.forEach(item => {
    const id = `${item.kind}/${item.file_id}`;
    groups.set(id, [...(groups.get(id) || []), item]);
  });
  function openPage(next: number) {
    location.hash = noteCenterLink({ query, page: next, view: "marks" });
  }
  return <>
    <p className="muted">集中查看保留版本中标记的重点，点击标题回到对应位置。</p>
    {!current ? <p role="status" className="note-search-state">正在读取重点…</p>
      : current.error ? <div role="status" className="note-search-state"><p>{current.error}</p>
        <button className="secondary" onClick={() => setAttempt(value => value + 1)}>重新读取重点</button></div>
      : result && <>
        <div className="note-center-status"><p role="status">{result.total ? `已标记 ${result.total} 处重点` : "还没有重点。在笔记章节或简洁小标题旁点击“标为重点”，就会出现在这里。"}</p>
          <button className="secondary" onClick={() => setAttempt(value => value + 1)}>刷新重点</button></div>
        {[...groups].map(([id, items]) => <section className="panel note-search-group" key={id}>
          <header><span className="eyebrow">{items[0].kind === "video" ? "视频" : "文档"}</span><h2>{items[0].filename}</h2></header>
          {items.map(item => {
            return <article className="note-search-hit" key={`${item.task_id}/${item.chapter_id}/${item.heading_index}`}>
              <span className="muted">★ 重点 · {item.version === 2 && "上一份笔记 · "}{item.note_title}
                {item.heading_index > 0 && ` · ${item.chapter_title}`}</span>
              <h3><a href={noteLocationLink(item.file_id, { taskId: item.task_id, chapterId: item.chapter_id, headingIndex: item.heading_index, returnTo: { query, page, view: "marks" } })}>{item.title}</a></h3>
              {item.snippet && <p>{item.snippet}</p>}
            </article>;
          })}
        </section>)}
        {result.total > result.page_size && <nav className="note-search-pagination" aria-label="重点列表分页">
          <button className="secondary" disabled={page <= 1} onClick={() => openPage(page - 1)}>上一页</button>
          <span>第 {page} / {Math.ceil(result.total / result.page_size)} 页</span>
          <button className="secondary" disabled={page * result.page_size >= result.total} onClick={() => openPage(page + 1)}>下一页</button>
        </nav>}
      </>}
  </>;
}
