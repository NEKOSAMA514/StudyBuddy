import { createContext, useContext, useId, useRef, type ComponentPropsWithoutRef, type ReactNode, type MouseEvent } from "react";
import Markdown, { type ExtraProps } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import rehypeHighlight from "rehype-highlight";
import "katex/contrib/mhchem";
import "katex/dist/katex.min.css";
import "../styles/markdown.css";
import { CodeBlock } from "./CodeBlock";
import { markdownAnchors } from "./markdownAnchors";

const HeadingAction = createContext<((index: number) => ReactNode) | undefined>(undefined);
// 只为已编号的正文二级标题附加重点操作，脚注生成标题保持原样。
function Subheading({ node, children, ...props }: ComponentPropsWithoutRef<"h2"> & ExtraProps) {
  const action = useContext(HeadingAction);
  const heading = <h2 {...props}>{children}</h2>;
  const index = Number(node?.properties.dataNoteHeading);
  return action && index > 0 ? <div className="note-heading">{heading}{action(index)}</div> : heading;
}

// 统一渲染 Markdown、公式和代码，局部锚点隔离，来源链接交给领域回调。
export default function NoteMarkdown({ children, onTimeJump, onSourceJump, density = "body", headingAction }: {
  children: string; onTimeJump?: (seconds: number) => void; density?: "body" | "source";
  onSourceJump?: (id: string) => void;
  headingAction?: (index: number) => ReactNode;
}) {
  const prefix = `markdown-${useId()}-`;
  const root = useRef<HTMLDivElement>(null);
  function navigate(event: MouseEvent<HTMLAnchorElement>) {
    event.preventDefault();
    const fragment = event.currentTarget.getAttribute("href")!.slice(1);
    const time = /^video-time-(\d+(?:\.\d+)?)$/.exec(fragment);
    if (onTimeJump && time && Number.isFinite(Number(time[1]))) {
      onTimeJump(Number(time[1]));
      return;
    }
    const source = /^document-unit-(D[1-9]\d*)$/.exec(fragment);
    if (onSourceJump && source) {
      onSourceJump(source[1]);
      return;
    }
    let target = document.getElementById(fragment);
    if (!target) {
      try { target = document.getElementById(decodeURIComponent(fragment)); } catch { return; }
    }
    if (!target || !root.current?.contains(target)) return;
    target.scrollIntoView({ block: "start", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    if (!target.hasAttribute("tabindex")) target.tabIndex = -1;
    target.focus({ preventScroll: true });
  }
  return <div className="note-markdown" data-density={density}><div className="markdown-content" ref={root}>
    <HeadingAction.Provider value={headingAction}><Markdown
    remarkPlugins={[remarkGfm, remarkMath]}
    rehypePlugins={[[rehypeKatex, { trust: false, maxSize: 10 }], [rehypeHighlight, { detect: false }], markdownAnchors(prefix, !!headingAction)]}
    skipHtml
    disallowedElements={["img"]}
    components={{
      h2: Subheading,
      pre: ({ node: _node, ...props }) => <CodeBlock {...props} />,
      table: ({ children }) => <div className="markdown-table"><table>{children}</table></div>,
      input: ({ checked }) => <input type="checkbox" checked={!!checked} disabled aria-label={checked ? "已完成" : "未完成"} />,
      a: ({ node: _node, children, href, ...props }) => href?.startsWith("#")
        ? <a {...props} href={href} className={onSourceJump && href.startsWith("#document-unit-") ? "note-source-link" : props.className} onClick={navigate}>{children}</a>
        : <a {...props} href={href} target="_blank" rel="noopener noreferrer">{children}</a>,
    }}
  >{children}</Markdown></HeadingAction.Provider></div></div>;
}
