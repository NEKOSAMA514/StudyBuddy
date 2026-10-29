import type { Element, Root } from "hast";

// 为每份 Markdown 的标题和脚注添加独立作用域，避免锚点相互冲突。
export function markdownAnchors(prefix: string, numberedHeadings = false) {
  return () => (tree: Root) => {
    const elements: Element[] = [];
    function collect(parent: Root | Element) {
      for (const child of parent.children) if (child.type === "element") {
        elements.push(child);
        collect(child);
      }
    }
    function text(node: Element): string {
      return node.children.map(child => child.type === "text" ? child.value : child.type === "element" ? text(child) : "").join("");
    }
    collect(tree);
    const ids = new Map<string, string>();
    const slugs = new Map<string, number>();
    let headingIndex = 0;
    for (const element of elements) {
      // 脚注生成的二级标题没有原文位置，仅为正文中的二级标题编号。
      if (numberedHeadings && element.tagName === "h2" && element.position) element.properties.dataNoteHeading = ++headingIndex;
      if (/^h[1-6]$/.test(element.tagName) && !element.properties.id) {
        const slug = text(element).trim().toLowerCase().replace(/[^\p{L}\p{N}_\s-]/gu, "").replace(/\s/g, "-");
        const count = slugs.get(slug) || 0;
        slugs.set(slug, count + 1);
        element.properties.id = count ? `${slug}-${count}` : slug;
      }
      const id = element.properties.id;
      if (typeof id === "string") {
        const scoped = prefix + id;
        ids.set(id, scoped);
        element.properties.id = scoped;
      }
    }
    function resolve(id: string) {
      if (ids.has(id)) return ids.get(id);
      try { return ids.get(decodeURIComponent(id)); } catch { return undefined; }
    }
    for (const element of elements) {
      const href = element.properties.href;
      if (typeof href === "string" && href.startsWith("#")) {
        const target = resolve(href.slice(1));
        if (target) element.properties.href = `#${target}`;
      }
      const describedBy = element.properties.ariaDescribedBy;
      if (Array.isArray(describedBy)) element.properties.ariaDescribedBy = describedBy.map(id => resolve(String(id)) || id);
    }
  };
}
