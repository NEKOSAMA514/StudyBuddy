import React from "react";
import { createRoot } from "react-dom/client";
import "./style.css";
import { App } from "./App";
import { ErrorBoundary } from "./diagnostics";
import { installErrorReporting } from "./api";

// 挂载界面前接入全局异常上报，渲染失败由错误边界保留可恢复页面。
installErrorReporting();
createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ErrorBoundary><App /></ErrorBoundary>
  </React.StrictMode>,
);
