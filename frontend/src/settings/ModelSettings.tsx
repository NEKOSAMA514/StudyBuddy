import { useEffect, useRef, useState } from "react";
import { RecognitionSettings } from "./RecognitionSettings";
import { NetworkSettings } from "./NetworkSettings";
import { NoteStyleSettings } from "./NoteStyleSettings";
import { APISettings } from "./APISettings";
import { MinerUSettings } from "./MinerUSettings";
import { CredentialSettings } from "./CredentialSettings";

// 按类别装配独立设置表单，切换类别时各表单自行清理未完成请求。
export function ModelSettings({ onSaved }: { onSaved: (close: boolean) => void }) {
  const [category, setCategory] = useState<"asr" | "ocr" | "llm" | "style" | "network" | "credentials">("llm");
  return <section className="model-settings">
    <div className="heading"><div>
      <h1 id="settings-title">设置</h1>
      <p>管理识别模型、语言模型、笔记风格、视频下载网络和网站登录凭证。</p>
    </div></div>
    <div className="model-category-tabs" role="tablist" aria-label="设置类别">
      {([ ["asr", "语音识别模型"], ["ocr", "OCR 模型"], ["llm", "API设置"], ["style", "笔记风格"], ["network", "网络代理"], ["credentials", "登录凭证管理"] ] as const).map(([id, label]) => (
        <button type="button" role="tab" id={`model-tab-${id}`} aria-controls="model-category-panel" aria-selected={category === id} key={id} onClick={() => setCategory(id)}>{label}</button>
      ))}
    </div>
    <div id="model-category-panel" role="tabpanel" aria-labelledby={`model-tab-${category}`}>
      {category === "credentials" ? <CredentialSettings /> : category === "network" ? <NetworkSettings /> : category === "style" ? <NoteStyleSettings onSaved={() => onSaved(false)} /> : category === "llm" ? <><APISettings onSaved={onSaved} /><MinerUSettings onSaved={() => onSaved(false)} /></> : <RecognitionSettings kind={category} onSaved={() => onSaved(false)} />}
    </div>
  </section>;
}

// 管理设置覆盖层的打开和关闭，底下的资料详情继续保留。
export function ModelSettingsDialog({
  onSaved,
  onClose,
}: {
  onSaved: (close: boolean) => void;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = dialog.current!;
    element.showModal();
    return () => element.close();
  }, []);
  return (
    <dialog
      ref={dialog}
      className="settings-dialog"
      aria-labelledby="settings-title"
      onCancel={onClose}
      onClick={(event) => {
        if (event.target === dialog.current) {
          const bounds = dialog.current!.getBoundingClientRect();
          if (
            event.clientX < bounds.left ||
            event.clientX > bounds.right ||
            event.clientY < bounds.top ||
            event.clientY > bounds.bottom
          )
            onClose();
        }
      }}
    >
      <button
        className="dialog-close"
        type="button"
        aria-label="关闭设置"
        onClick={onClose}
      >
        <svg viewBox="0 0 20 20" aria-hidden="true"><path d="m5 5 10 10M15 5 5 15" /></svg>
      </button>
      <ModelSettings onSaved={onSaved} />
    </dialog>
  );
}
