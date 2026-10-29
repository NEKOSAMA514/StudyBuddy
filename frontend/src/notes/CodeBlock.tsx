import { isValidElement, useState, type ComponentPropsWithoutRef, type ReactNode } from "react";

// 从高亮后的嵌套节点还原原始代码文字，复制时不包含工具栏内容。
function codeText(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(codeText).join("");
  if (isValidElement<{ children?: ReactNode }>(node)) return codeText(node.props.children);
  return "";
}

// 将代码、语言标签和复制反馈分开显示，长代码在自身区域滚动。
export function CodeBlock({ children, ...props }: ComponentPropsWithoutRef<"pre">) {
  const code = isValidElement<{ className?: string; children?: ReactNode }>(children) ? children.props : null;
  const text = codeText(code?.children);
  const language = code?.className?.match(/(?:^|\s)language-([^\s]+)/)?.[1];
  const [feedback, setFeedback] = useState({ text: "", message: "" });
  if (!code) return <pre {...props}>{children}</pre>;

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setFeedback({ text, message: "已复制" });
    } catch {
      setFeedback({ text, message: "复制失败，请重试" });
    }
  }

  return <div className="note-code-block">
    <div className="note-code-toolbar">
      <span className="note-code-language">{language || "代码"}</span>
      <button type="button" onClick={() => void copy()} aria-label="复制代码">
        <span role="status">{feedback.text === text && feedback.message ? feedback.message : "复制"}</span>
      </button>
    </div>
    <pre {...props} tabIndex={0} aria-label="代码内容">{children}</pre>
  </div>;
}
