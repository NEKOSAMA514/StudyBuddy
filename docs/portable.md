# Windows x64 CPU 便携包

首版交付本机验证预览。目标为 Windows 10/11 x64，尚不宣称干净系统、真实云端 API 或公开分发许可审查完成。项目源码 MIT 不覆盖第三方二进制、库和模型权重。

新构建使用 StudyBuddy 名称；更名前生成的历史 ZIP 和验收记录保留原样，不代表新名称的运行包已经重建。发布时需按当前源码重新构建并验收。

## 使用与升级

完整解压原始 ZIP 到固定、可写目录，双击 `启动.cmd`。首次准备独立 Python 后自动打开本机页面。路径支持中文和空格；修复后不能移动或重命名程序目录及其父目录。移动时重新解压原始 ZIP，不反复修补旧环境。[conda-pack 文档](https://conda.github.io/conda-pack/)说明修复后的环境不能再次搬移。

默认 CPU Paraformer、RapidOCR、FFmpeg/FFprobe、Deno 与 yt-dlp-ejs 已附带。Whisper 在页面按需下载。新用户在页面填写 API 凭据。默认端口 18000，被占用时选择 18000—18019 中的空闲端口，只监听 `127.0.0.1`。同目录已有实例就绪时打开它的页面；准备或退出期间提示等待。

控制台显示进度和错误，首次路径修复和检查全部成功后才原子写入成功标记。服务健康等待上限 120 秒；就绪检查结合持有的监听套接字、服务状态和系统端口所属进程，不打开其他服务。浏览器失败时显示手动地址并保留服务。失败返回非零状态，命令窗口保留错误。

按一次 Ctrl+C 正常停机，等待既有 Worker、检查点和下载子进程清理。Windows 若在服务完成退出后询问是否结束批处理，输入 Y 确认。关闭浏览器不会停止后台服务，不增加远程退出接口。原有 HTTP、数据库、任务和缓存版本保持不变。便携启动器使用已通过批量浏览器请求及退出验收的 Windows Selector 事件循环，避开本机默认 Proactor 循环的停机挂起。

升级时先正常停机，备份 `data/`、`log/`；新目录完整解压新版后迁移资料、日志和可选 Whisper 模型。不要覆盖新版 `runtime/`、工具或默认权重。凭据仍沿用 Windows DPAPI，同机同账户恢复；换电脑或账户后重新填写。详见 [存储与恢复](storage.md) 和包内 `使用说明.txt`。

## 构建

需要 Windows x64、64 位 PowerShell、Conda、Node/npm、Git 和网络。从仓库根目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts/build-portable.ps1 -Version 0.1.0
```

不在 PATH 的 Conda 可用 `-CondaExe <完整路径>` 指定。Python 固定 3.12.15，构建工具使用独立 venv 中的 `conda-pack==0.9.2`（[版本来源](https://pypi.org/project/conda-pack/)）。基础运行库从 conda-forge 安装，确切版本、构建、来源记录在 `licenses/conda-explicit.txt` 和清单中。后续可传 `-CondaExplicit <此前的 conda-explicit.txt>` 重用该层精确版本；后端与前端继续按仓库锁文件安装。

构建缓存和临时环境均位于本次独有的 `dist/.build/<编号>/`，不注册到现有 Conda 环境列表，不修改用户配置。工作目录和日志保留供定位失败；脚本不进行递归清理。同名 ZIP 或校验文件存在时停止，不覆盖旧产物。

收集规则是正向清单：后端仅 Git 跟踪的 `backend/app/**/*.py`；前端仅 Git 跟踪的构建配置、`src/` 和 `public/`，临时安装并生产构建。开发目录增加其他文件不会改变收集结果；不递归扫描其他本地目录。版本和未提交状态也随清单记录，未提交的新增构建输入记录摘要。

现有 FFmpeg 锁定 ZIP 经 SHA-256 验证后只提取 FFmpeg、FFprobe、许可和 README；缺少时构建阶段下载固定来源。Paraformer 只收集目录定义的两个文件并沿用现有模型和 tokens 校验；RapidOCR 权重直接取自锁定 wheel。不会复制整个 `models/`。

```text
StudyBuddy-Windows-x64/
  启动.cmd、launcher.py、使用说明.txt
  LICENSE、THIRD_PARTY_NOTICES.txt、release-manifest.json
  runtime/、backend/app/、web/、tools/、models/、licenses/
```

ZIP 不含 `.env`、用户 `data/`、`log/`、测试、报告、实验、档案、编辑器设置、Git 目录或下载缓存。根 `debug.log` 只停止跟踪并忽略，保留本地内容和既有历史。运行库自身的必要测试模块和库内资源可能随第三方包存在，不等于项目测试被收集。

运行环境通过 conda-pack 打包后只解压入暂存目录，不执行路径修复。所有验证在原始 ZIP 的独立副本中进行。清单记录包内文件 SHA-256、尺寸、依赖、模型、工具与来源；清单不递归包含自身摘要，ZIP 外部 SHA-256 覆盖整个包。输出为：

```text
dist/StudyBuddy-v0.1.0-windows-x64-preview.zip
dist/StudyBuddy-v0.1.0-windows-x64-preview.zip.sha256
```

第三方说明集中在包内 `THIRD_PARTY_NOTICES.txt`，原始许可和模型授权保存在 `licenses/`。构建脚本补收已确认缺失的四份许可证和 ONNX Runtime 第三方声明，保留模型来源及 RapidOCR 的权重授权说明。依赖版本和源码出处见包内清单；FFmpeg 所有外部依赖及实际构建材料尚未全部取得，不能把现有的本体源码或来源链接表述为完整对应源码。该项保留说明，不为所有依赖新增源码下载或版本阻断流程。[GitHub Releases](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)要求每个附件小于 2 GiB。

## 验证与记录

启动器和构建边界测试不依赖已有后端环境：

```powershell
python -B -m unittest discover -s scripts/portable -p test_portable.py -v
```

本机验收记录和原始报告仅在本地保留，不随源码分发。干净系统按 [验收清单](portable-clean-machine.md)逐台执行，不能把开发机结果当作干净系统结果。既有回归应在专用环境使用隔离资料运行；浏览器只选择首启、上传、提取、设置保存和恢复等关键场景。

另有 [本机验收工具](../scripts/portable/validate.py)：先校验并独立解压 ZIP，再使用该副本的 `runtime/python.exe -I -B -X utf8 scripts/portable/validate.py --package <独立解压目录> --output <包外报告目录> --repo . --speech <独立合成中文语音.wav>`。它要求未经初始化的副本，验证清单后实际执行首次准备失败与重试、重复启动、端口回退、Ctrl+C、设置重启恢复及本地能力。语音样本需含“学习”“三角”或“数学”之一，本轮用 Windows Microsoft Huihui Desktop 在包外合成；图片使用 Windows 微软雅黑。测试素材只写入报告目录。此工具不调用真实云端 API，不操作开发资料。首次修复只能对独立副本执行，不能对构建暂存目录执行。
