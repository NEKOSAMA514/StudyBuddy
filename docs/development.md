# 开发与测试

本页用于接手代码、选择修改入口和验证范围。首次安装见 [安装与使用](usage.md)，模块关系见 [架构](architecture.md)，公开语义见 [契约](contracts.md)。本机协作约定保存在根目录 AGENTS.md，该文件不随源码分发。

## 本机开发方式

Windows 便携包统一入口为 [build-portable.ps1](../scripts/build-portable.ps1)，例如 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-portable.ps1 -Version 0.1.0`。构建会在忽略的 `dist/` 中新建专用 Python 环境、工具环境和前端工作副本，不覆盖当前环境或 `frontend/dist/`。依赖、来源、文件清单和公开发布前待办随包记录；详细收集规则及验证入口见 [便携包说明](portable.md)。

先准备可启动的后端环境和一次前端构建。当前本机验证入口为 [start-local.ps1](../scripts/start-local.ps1) 和 [run-tests-local.ps1](../scripts/run-tests-local.ps1)；仓库中的旧容器测试入口不作为当前 Windows 流程的替代。

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

PDF 依赖固定为 `pypdfium2==5.14.0`、`pypdf[crypto]==6.21.3`，不安装 PyMuPDF、pymupdf-layout 或 pymupdf4llm。共享样本使用独立的 [PDF 内容流构造工具](../backend/tests/pdf_samples.py)，浏览器 MinerU 替身用 pypdf 读取。主要回归为 `test_pdf_text.py`、`test_pdf_images.py`、`test_pdf_lifecycle.py`、文档图片、MinerU 与导入恢复测试；文字允许无语义空白差异，图片尺寸、像素、透明度、来源页码与识别调用次数仍精确断言。锁文件和共享夹具变化后需运行完整后端；浏览器覆盖文档上传、提取、阅读和恢复，再检查文档。

2026-10-29 的替换验收在 Windows／Python 3.12、不含旧 PDF 库的独立环境完成：完整后端 1775 项通过；文档相关浏览器 146 项通过，2 项按原有视口去重规则跳过，对应阅读矩阵已在另一视口执行。25 份旧 PDF 样本的正文、顺序、来源及图片像素对照无差异，另有 11 组不同尺寸、渐变、单行／单列蒙版的固定像素回归。依赖检查通过；锁文件补齐了原清单已有的同版本 `qrcode` 及其 Windows 依赖，未升级其他现有包。

该验收不包含真实 LLM、MinerU 或 OCR 服务的效果、正式资料迁移、其他系统平台、任意复杂字体及版面的穷尽兼容，也未验证便携包或修改许可证文件。已有材料和恢复兼容策略见 [存储说明](storage.md#缓存检查点与版本)。

DPAPI 相关测试需要能够访问当前账户密钥的 Windows 进程，使用隔离测试凭据。不能通过明文回退、跳过失败场景或修改正式凭据来取得通过结果。

| 结果 | 常见位置 |
|---|---|
| 本次执行状态与套件范围 | `summary.json` |
| 后端结果 | `backend.xml`、`backend.log` |
| 文档检查 | `docs.json`、`docs.log` |
| 构建模式浏览器 | `browser/results.json`，以及截图、trace、HTML 报告 |
| 源码模式浏览器 | `browser-source/results.json` 及对应产物 |

生产只读 Smoke 入口针对已运行的本机服务，属于单独检查，不能代替隔离回归或默认参与文档维护。历史结果可用于定位回归证据，但结果只覆盖报告注明的代码与范围；当前文档不滚动累积历史通过总数。

## Git 收录范围

仓库保留应用源码、正式测试与夹具、开发和构建脚本、依赖清单与锁文件、公开文档和 LICENSE。Docker、Compose 和 Conda 定义同样属于开发材料，不按运行产物忽略。

| 文件类别 | Git 处理 |
|---|---|
| 后端 `app/`、`tests/`、`scripts/`，前端 `src/`、`public/`、`tests/`、`scripts/` 及构建配置 | 收录 |
| 根目录 `scripts/`、依赖文件、README、公开 `docs/`、LICENSE 及 Git／构建配置 | 收录 |
| 私人配置、用户资料、模型、媒体工具、依赖安装目录、构建产物、报告、缓存与日志 | 按 `.gitignore` 排除，本地保留 |
| `ocr-test/`、`documents-test/`、`analysis test/`、`test video/`、历史归档和本机协作记录 | 仅本地保留 |
| `docs/portable-validation.md` 和 `dist/portable-validation/` | 本机验收证据，仅本地保留 |

便携 ZIP 和 SHA-256 作为发布附件提供，仓库只保留构建及验证方法。提交前查看 `git status` 和 `git diff --cached --name-only`，确认新增源码已纳入跟踪，且暂存清单不含本地产物。修改忽略规则不会清除既有提交历史。

## 维护这套文档

当前入口为 README 和五篇专题：操作写入 usage，设计与数据流写入 architecture，开发和测试写入本页，公开语义及默认限制写入 contracts，文件及恢复写入 storage。修改同一规则时更新唯一归属页，其余位置使用链接。

根目录 PROJECT_CONTEXT 是本机交接状态，历史归档位于本地 archive；它们不随仓库分发，不应成为入库文档的必需链接。实验目录和测试报告继续原位保存，不能把某次实验结论直接写成产品能力。

文档检查工具 [check_docs.py](../scripts/check_docs.py) 核对 README 和公开专题的本地链接、围栏、JSON 示例及编码；本地存在 AGENTS.md 时一并检查，不检查本机验收记录。它不请求外部网站，也不保证链接锚点、命令效果或业务描述与代码一致，这些仍需对照源文件检查。

每段操作先说明前提，再给步骤与预期结果；不复制多份字段表，不追加“本轮修改／未提交”的历史流水账。安装说明不绑定开发者的私人盘符，配置示例不包含真实凭据。

组织方式参考 [GitHub README 指南](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes)和 [Diátaxis](https://diataxis.fr/)：让入口、操作、技术参考与解释各自回答一个明确问题。
