# 开发与测试

本页用于接手代码、选择修改入口和验证范围。首次安装见 [安装与使用](usage.md)，模块关系见 [架构](architecture.md)，公开语义见 [契约](contracts.md)。

## 本机开发方式

Windows 便携包的构建命令、输入和验收步骤见 [便携包说明](portable.md)。

先准备后端环境并构建一次前端。本机启动和测试分别使用 [start-local.ps1](../scripts/start-local.ps1) 和 [run-tests-local.ps1](../scripts/run-tests-local.ps1)。

在两个已激活 Conda 环境的终端中分别运行，起始位置均为仓库根目录。

后端终端：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start-local.ps1 -Reload
```

前端终端：

```powershell
cd frontend
npm run dev
```

开发页默认使用 `http://localhost:5173`，将 API 代理到本机后端；正式页面默认在后端端口提供 `frontend/dist`。开发源文件热更新与正式构建不是同一份输出，后端启动脚本仍要求已有前端构建。端口与代理以 [vite.config.ts](../frontend/vite.config.ts) 和启动脚本为准。

语音模型、识别运行库、模型 API 和 MinerU 不是前端开发服务器提供的功能；开发页面也需要对应后端。调整凭据存储或启动路径时使用隔离数据目录，不用正式服务试验迁移。

## 从哪一段代码开始

| 修改内容 | 优先入口 |
|---|---|
| 服务装配、生命周期、共享依赖 | [后端 main](../backend/app/main.py)、[HTTP 边界](../backend/app/http.py) |
| 视频登记、任务控制和执行 | [视频服务](../backend/app/videos/video_service.py)、[任务服务](../backend/app/videos/analysis_service.py)、[Pipeline](../backend/app/videos/pipeline.py) |
| 文档上传、后台提取与任务 | [Store](../backend/app/documents/store.py)、[Imports](../backend/app/documents/imports.py)、[文档任务](../backend/app/documents/tasks.py) |
| 模型请求和输出规则 | [共用客户端](../backend/app/analysis/llm.py)、[预算](../backend/app/analysis/budgets.py)、视频／文档各自的 prompts 和生成器 |
| 设置、执行快照和加密 | [设置服务](../backend/app/settings/settings_service.py)、[连接快照](../backend/app/settings/model_profiles.py)、[私人存储](../backend/app/util/private_storage.py) |
| 页面装配与文件状态 | [App](../frontend/src/App.tsx)、[文件 Hook](../frontend/src/files/useFiles.tsx)、[文档导入 Hook](../frontend/src/documents/useDocumentImport.ts) |
| 任务恢复与阅读 | [任务跟踪](../frontend/src/shared/useTaskTracking.ts)、[作用域读取](../frontend/src/shared/useScopedRead.ts)、[笔记版本](../frontend/src/notes/useNoteVersions.ts) |
| 样式与正文渲染 | [样式入口](../frontend/src/style.css)、[样式目录](../frontend/src/styles)、[Markdown](../frontend/src/notes/NoteMarkdown.tsx) |

读流程时可先跟随一次视频上传到 `analyze → Worker → Pipeline`，再沿文档的 `Store → Imports → DocumentAnalyses → DocumentPipeline` 对照。任务、证据、材料和正文的职责不要只按相似函数名合并。

前端 HTTP 解析集中在 [api.ts](../frontend/src/api.ts)，上传进度仍用 XMLHttpRequest 并复用相同解析规则。类型、通知和 B 站会话有独立业务入口；业务请求不从弹窗组件导入契约。样式先查已有主题变量和通用控件，区域文件负责布局，响应式覆盖在最后加载。

## 按改动范围选择验证

优先执行能覆盖改动行为的最小检查。只有共享机制影响广、相关测试暴露连锁问题或明确要求时，才扩大到完整回归；仅改文档不默认启动服务、构建页面或运行全部业务测试。

自动 CI 在推送到 `main` 或创建、更新目标为 `main` 的 PR 时运行。若改动全部属于根目录 `README.md` 或 `docs/` 下的 Markdown 文档，则跳过自动 CI；混有代码、配置或其他文件时仍运行完整检查。其他目录的 Markdown 测试样本不在忽略范围内。手动启动 CI 和 CD 调用的完整检查不受这项文件筛选影响。

首次运行浏览器回归时，先在 `frontend` 目录执行 `npx --no-install playwright install chromium`。`npm ci` 安装测试工具，浏览器本体需另行下载；它只用于回归，不是日常启动的前提。

下列命令在已激活环境的仓库根目录运行：

| 检查范围 | 命令 |
|---|---|
| 文档 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-tests-local.ps1 -Suite Docs` |
| 相关后端文件 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-tests-local.ps1 -Suite Backend -BackendTests tests/test_mineru.py` |
| 完整后端 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-tests-local.ps1 -Suite Backend` |
| 类型与隔离构建 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-tests-local.ps1 -Suite Frontend` |
| 相关构建模式浏览器 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-tests-local.ps1 -Suite Browser -BrowserTests settings-lifecycle.spec.ts` |
| 独立源码错误处理 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-tests-local.ps1 -Suite BrowserSource -BrowserTests error-reporting.spec.ts` |
| 完整前端浏览器入口 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-tests-local.ps1 -Suite Browser` |

单独检查类型可在 `frontend` 中运行 `node node_modules/typescript/bin/tsc -b`。本机测试脚本的后端与浏览器文件参数支持逗号分隔；传入额外筛选参数时注意 PowerShell 与原生命令的转义，不要把未执行到目标用例误记为通过。

`Browser` 会先隔离构建；不指定文件时运行全部构建模式用例，并随后运行默认的源码错误处理套件。`BrowserSource` 启动独立 Vite 并执行源码模式。源码专用用例在构建模式的跳过不代表已验证，必须实际运行源码入口。

浏览器项目的既有视口矩阵、去重和跳过条件以 [Playwright 配置](../frontend/playwright.config.ts) 和测试文件为准。报告分别记录通过、失败、跳过与重试；定向复跑不能覆盖未执行部分的结论，也不把多轮重复执行数当作不同用例数。

## 测试数据与结果边界

本机脚本为每次运行生成 `test-results/local-<时间>-<编号>/`，设置独立的 DATA_DIR、MODEL_DIR、LOG_DIR 和 STATIC_DIR；构建写入报告目录下的 `web`，不覆盖正式 dist。浏览器使用测试工厂、临时媒体和替身，并检查隔离身份。

普通回归不调用真实模型、MinerU、ASR/OCR 服务或平台下载；替身返回和网络限制由对应测试控制。真实识别效果、真实云端模型质量、平台访问与浏览器全流程是否验证，应分别说明，不能从替身通过推导实际效果。

主要 PDF 回归包括 `test_pdf_text.py`、`test_pdf_images.py`、`test_pdf_lifecycle.py`、文档图片、MinerU 与导入恢复测试。文字允许无语义空白差异，图片尺寸、像素、透明度、来源页码与识别调用次数精确断言。共享样本由测试工具生成，浏览器 MinerU 替身用 pypdf 读取。[PDF 内容流构造工具](../backend/tests/pdf_samples.py)

锁文件和共享夹具变化后需运行完整后端测试。浏览器覆盖文档上传、提取、阅读和恢复，再检查文档。已有材料的兼容与恢复规则见 [存储说明](storage.md#缓存检查点与版本)。

DPAPI 相关测试需要能够访问当前账户密钥的 Windows 进程，使用隔离测试凭据。不能通过明文回退、跳过失败场景或修改正式凭据来取得通过结果。

| 结果 | 常见位置 |
|---|---|
| 本次执行状态与套件范围 | `summary.json` |
| 后端结果 | `backend.xml`、`backend.log` |
| 文档检查 | `docs.json`、`docs.log` |
| 构建模式浏览器 | `browser/results.json`，以及截图、trace、HTML 报告 |
| 源码模式浏览器 | `browser-source/results.json` 及对应产物 |

对已运行的本机服务，可用 `-Suite Smoke` 执行只读检查。隔离回归使用上文的后端和浏览器套件。

## Git 收录范围

仓库收录应用源码、正式测试与夹具、开发和构建脚本、依赖清单与锁文件、公开文档和许可证。Docker、Compose 和 Conda 定义也属于开发材料。忽略规则覆盖私人配置、用户资料、下载的模型和工具、依赖安装目录、构建产物、报告、缓存与日志。[Git 忽略规则](../.gitignore)

便携 ZIP 和 SHA-256 文件作为发布附件提供。提交前用 `git status` 和 `git diff --cached --name-only` 核对准备提交的文件。

## 维护这套文档

同一条规则在归属页维护，其他页面通过链接引用。比如修改视频时长上限时，在接口契约更新具体数值，使用指南需要提及时链接过去。

| 更新内容 | 归属文档 |
|---|---|
| 项目用途、启动入口和文档导航 | [README](../README.md) |
| 安装、设置和使用步骤 | [安装与使用](usage.md) |
| 模块职责、处理流程和数据流 | [架构与处理流程](architecture.md) |
| 接口语义、默认值和限制 | [接口与运行契约](contracts.md) |
| 数据文件、兼容、备份和恢复 | [存储与恢复](storage.md) |
| 开发流程和验证方法 | 本页 |
| 便携包使用、升级与构建 | [Windows 便携包](portable.md) |
| 独立 FFmpeg 的构建和使用 | [FFmpeg 构建](ffmpeg-build.md) |
| 对应源码和第三方组件来源 | [源码说明](SOURCE.md) |

操作说明写清前提、步骤和预期结果。命令与配置示例使用通用路径和占位值。公开文档引用读者可访问的仓库文件或公开资料。版本变更写入发布说明，单次验收结果留在对应报告里。

改完运行本页的 `-Suite Docs` 检查。检查工具核对本地文件链接、代码围栏、JSON 示例及编码。标题跳转另行检查，命令效果和业务描述需对照脚本与实现。[check_docs.py](../scripts/check_docs.py)

README 的入口设计和专题分工可参考这两份资料。[GitHub README 指南](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes) · [Diátaxis](https://diataxis.fr/)
