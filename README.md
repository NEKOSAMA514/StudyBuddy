# StudyBuddy

**将课程视频、课件和文档整理成能回看来源的学习笔记。**

![Frontend](https://img.shields.io/badge/frontend-React%20%2B%20TypeScript-61DAFB?style=flat-square&logo=react)
![Backend](https://img.shields.io/badge/backend-FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square&logo=python&logoColor=white)
[![License](https://img.shields.io/badge/license-GPLv3-22c55e?style=flat-square)](LICENSE)

StudyBuddy 是一个在本机运行的学习资料笔记工具。导入视频、PDF 或课件后，使用本地识别提取内容，再通过你配置的模型 API 生成笔记。

阅读时，可以跳回视频中的对应时间点，查看文档的提取内容与来源位置；复习时，可以搜索笔记、标记重点，或导出 Markdown 和纯文本。

**Windows x64 · 本地运行 · 视频与文档 · GPLv3**

[功能特性](#功能特性) · [开发与构建](#开发与构建) · [使用文档](#使用文档)

## 功能特性

| 功能 | 说明 |
|---|---|
| 🎬 视频笔记 | 支持上传本地视频、通过链接下载导入，结合字幕或本地语音转写与画面文字生成笔记 |
| 📄 文档笔记 | 支持 PDF、Word、PowerPoint、Excel、CSV、TXT 和 Markdown |
| 🔎 来源核对 | 视频笔记可跳转到对应时间；文档内容保留页码、幻灯片、正文块或行范围等来源信息 |
| 📝 两种风格 | 选择简洁或详尽笔记，补充要求可在设置中保存 |
| ⏯️ 停止与继续 | 支持停止、继续和重试；生成中可以先阅读已保存的章节 |
| 📚 阅读与复习 | 支持章节导航、笔记搜索和重点标记 |
| 🕘 版本与导出 | 可切换最近两份完整成功笔记，导出 Markdown 或 TXT |
| ⚙️ 自选模型 | 支持多组兼容 Chat Completions 的 API 配置，分别保存模型与生成参数 |

### 视频处理

- 支持本地视频，以及下载器能够解析的视频链接。
- B 站支持 BV 号、普通链接、短链接和分享文案；多 P 视频每次选择一 P。
- B 站字幕可用时优先采用，否则按任务配置进行本地语音识别。
- 识别抽样画面中的文字，补充语音中没有讲出的内容。
- 阅读笔记时，可通过时间入口回看视频，通过依据入口核对识别材料。

其他站点的可用性取决于下载器支持情况、访问权限和平台限制。

### 文档处理

| 格式 | 支持内容 |
|---|---|
| PDF | 本地文本与图片提取，或选择 MinerU 解析 |
| DOCX | 正文、表格和内嵌图片 |
| PPTX | 幻灯片文字、备注、基本表格、图片和支持的分类图表 |
| XLSX | 工作表内容、行号和公式原文 |
| CSV | 按逻辑记录提取内容 |
| TXT / Markdown | 按原行号组织材料 |

提取完成后即可查看材料。生成失败或预检未通过时，已保存的提取内容仍可阅读。

复杂排版、公式、扫描页及特殊图表可能无法完整还原，请结合来源核对。

## 本地处理与数据说明

原文件、提取材料、笔记和设置保存在本机：

- 视频语音转写和画面 OCR 使用本地识别组件。
- 生成笔记时，会把材料文字和标题等信息发送给你配置的模型 API。
- 选择 MinerU 解析 PDF 时，会将 PDF 批次上传到对应服务。
- 笔记中心搜索不调用模型 API。
- API 密钥、MinerU Token 和 B 站凭据使用 Windows 当前账户 DPAPI 加密保存；更换电脑或 Windows 账户后通常需要重新填写。

备份、升级和凭据恢复见 [存储与恢复](docs/storage.md)。

## 常见问题

### 需要独立显卡吗？

默认便携包使用 CPU，无需独立显卡或 CUDA。处理速度取决于电脑性能、资料长度和所用模型。

### 可以完全离线使用吗？

本地识别不依赖云端识别服务。使用云端模型生成笔记、使用 MinerU 解析，以及下载网络视频时需要联网。

### 生成中断后要重新开始吗？

任务支持停止、继续和重试，恢复时可复用符合当前规则的缓存和已保存章节。修改模型连接地址、生成规则或材料后，可能需要重新生成，页面会给出提示。

### 支持 macOS、Linux 或多人访问吗？

当前完整使用流程主要面向 Windows 单用户本机使用，尚未提供 macOS、Linux 发行包，也没有多用户权限系统。仓库保留容器配置作为开发材料，当前使用和验证主路径为 Windows。

## 开发与构建

主要技术栈：

| 部分 | 技术 |
|---|---|
| 前端 | React、TypeScript、Vite |
| 后端 | FastAPI、SQLAlchemy、SQLite |
| 语音识别 | Paraformer、faster-whisper |
| 画面文字识别 | RapidOCR |
| 媒体处理 | FFmpeg、FFprobe |
| 本地 PDF 处理 | pypdfium2、pypdf |

构建 Windows 便携包，需要 Windows x64、64 位 PowerShell、Conda、Node.js/npm、Git 和网络。

在 Git 仓库根目录执行，例如构建下一版：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts/build-portable.ps1 `
  -Version 0.1.1
```

脚本会创建独立运行环境、构建前端、收集工具和默认模型，并在 `dist` 下生成 ZIP 与 SHA-256 校验文件。

新增源码应先纳入 Git 跟踪；依赖变化时同步更新锁文件。构建成功后，仍需验证解压后的程序。

详细开发、测试及收录规则见 [开发与测试](docs/development.md)。

<details>
<summary>使用文档</summary>

| 文档 | 内容 |
|---|---|
| [安装与使用](docs/usage.md) | 安装、首次设置、资料处理、阅读和导出 |
| [Windows 便携包](docs/portable.md) | 便携包使用、构建和升级 |
| [架构与处理流程](docs/architecture.md) | 模块职责、数据流和关键设计 |
| [开发与测试](docs/development.md) | 开发环境、测试入口和 Git 收录范围 |
| [接口与运行契约](docs/contracts.md) | 任务状态、来源、配置快照和处理限制 |
| [存储与恢复](docs/storage.md) | 数据目录、缓存、凭据、备份和恢复 |
| [干净系统验收](docs/portable-clean-machine.md) | Windows 干净环境验证清单 |

</details>

<details>
<summary>问题反馈与贡献</summary>

欢迎反馈使用问题、提出功能建议或提交改进。

报告问题时，请说明系统版本、程序版本、操作步骤、预期结果和实际表现。附上错误提示或经过检查的日志，有助于复现问题。

提交代码前，请运行与修改范围相关的测试。不要提交真实密钥、私人资料、模型权重、运行日志或构建产物。

</details>

<details>
<summary>License</summary>

项目自有源码采用 [GNU General Public License v3.0](LICENSE)（`GPL-3.0-only`），Copyright (c) 2026 NEKOSAMA。

第三方库、工具和模型分别遵循各自许可。便携包附带相应声明与许可材料。

本项目及第三方组件的对应源码获取方式见 [源码说明](docs/SOURCE.md)。

</details>
