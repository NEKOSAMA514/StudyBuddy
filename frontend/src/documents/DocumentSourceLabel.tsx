import { useEffect, useId, useRef, useState } from "react";

// 默认收起较长来源列表，展开后保留全部来源标签供回看。
export function DocumentSourceLabel({ labels }: { labels: string[] }) {
  const text = labels.join("；");
  const id = useId();
  const label = useRef<HTMLParagraphElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [overflow, setOverflow] = useState(false);
  useEffect(() => {
    const element = label.current!;
    const measure = () => setOverflow(element.scrollWidth > element.clientWidth ||
      element.scrollHeight > Number.parseFloat(getComputedStyle(element).lineHeight) + 1);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [text]);
  return <div className="document-source-summary">
    <p ref={label} id={id} className="muted document-source-label" data-expanded={expanded}>来源：{text}</p>
    {overflow && <button type="button" className="text-button document-source-toggle" aria-controls={id}
      aria-expanded={expanded} aria-label={expanded ? "收起来源" : "展开来源"} onClick={() => setExpanded(value => !value)}>
      {expanded ? "收起" : "展开"}</button>}
  </div>;
}
