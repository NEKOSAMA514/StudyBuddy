import { useEffect, useRef, useState } from "react";

import type { LibraryFile } from "../types";

type ManagedFile = Pick<LibraryFile, "id" | "filename" | "pinned">;
export type FileAction = "rename" | "pin" | "delete";

// 展示文件操作菜单，监听外部点击和关闭事件以回收菜单状态。
export function FileMenu({ file, disabled, onAction }: {
  file: ManagedFile;
  disabled: boolean;
  onAction: (action: FileAction) => void;
}) {
  const [position, setPosition] = useState<{ top: number; left: number } | null>(null);
  const container = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!position) return;
    menu.current?.querySelector("button")?.focus({ preventScroll: true });
    const outside = (event: PointerEvent) => {
      if (!container.current?.contains(event.target as Node)) setPosition(null);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") { setPosition(null); trigger.current?.focus(); }
    };
    const close = () => setPosition(null);
    const anchor = trigger.current?.getBoundingClientRect();
    const scroll = () => {
      const box = trigger.current?.getBoundingClientRect();
      if (box?.top !== anchor?.top || box?.left !== anchor?.left) close();
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    window.addEventListener("resize", close);
    window.addEventListener("scroll", scroll, true);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape);
      window.removeEventListener("resize", close);
      window.removeEventListener("scroll", scroll, true);
    };
  }, [position]);
  return (
    <div className="file-actions" ref={container}>
      <button className="file-menu-trigger" ref={trigger} disabled={disabled}
        aria-label={`管理文件：${file.filename}`} aria-expanded={!!position}
        onClick={() => {
          if (position) { setPosition(null); return; }
          const box = trigger.current!.getBoundingClientRect();
          setPosition({ top: Math.min(box.bottom + 4, window.innerHeight - 134), left: Math.max(8, box.right - 132) });
        }}>⋯</button>
      {position && (
        <div className="file-action-menu" ref={menu} style={position} aria-label="文件操作">
          {(["rename", "pin", "delete"] as const).map(action => (
            <button key={action} className={action === "delete" ? "danger-text" : ""}
              onClick={() => { setPosition(null); onAction(action); }}>
              {action === "rename" ? "重命名" : action === "pin" ? file.pinned ? "取消置顶" : "置顶" : "删除"}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// 独立管理重命名或删除确认，提交中的关闭和重复操作交由调用方限制。
export function FileEditDialog({ file, action, busy, onClose, onConfirm }: {
  file: ManagedFile;
  action: "rename" | "delete";
  busy: boolean;
  onClose: () => void;
  onConfirm: (name: string) => Promise<void>;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [name, setName] = useState(file.filename);
  useEffect(() => {
    dialog.current?.showModal();
    return () => dialog.current?.close();
  }, []);
  return (
    <dialog className="file-edit-dialog" ref={dialog} aria-labelledby="file-edit-title"
      onCancel={event => { event.preventDefault(); if (!busy) onClose(); }}>
      <form onSubmit={event => { event.preventDefault(); void onConfirm(name.trim()); }}>
        <h2 id="file-edit-title">{action === "rename" ? "重命名文件" : "删除文件？"}</h2>
        {action === "rename" ? (
          <label>文件名称<input autoFocus value={name} maxLength={200} required disabled={busy}
            onChange={event => setName(event.target.value)} /></label>
        ) : <p>将删除“{file.filename}”及其本地原文件和处理结果，无法撤销。</p>}
        <div className="dialog-actions">
          <button type="button" className="secondary" disabled={busy} onClick={onClose}>取消</button>
          <button type="submit" className={action === "delete" ? "danger-button" : "primary"}
            disabled={busy || (action === "rename" && !name.trim())}>
            {busy ? "处理中…" : action === "rename" ? "保存名称" : "确认删除"}
          </button>
        </div>
      </form>
    </dialog>
  );
}
