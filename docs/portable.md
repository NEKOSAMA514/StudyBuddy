# Windows x64 CPU 便携包

便携包面向 Windows 10/11 x64。项目自有源码采用 GPLv3（GPL-3.0-only），第三方二进制、库和模型权重保留各自许可。

## 使用与升级

完整解压原始 ZIP 到固定、可写目录，双击 `启动.cmd`。首次准备独立 Python 后自动打开本机页面。路径支持中文和空格；修复后不能移动或重命名程序目录及其父目录。移动时重新解压原始 ZIP，不反复修补旧环境。[conda-pack 文档](https://conda.github.io/conda-pack/)说明修复后的环境不能再次搬移。

默认 CPU Paraformer、RapidOCR、FFmpeg/FFprobe、Deno 与 yt-dlp-ejs 已附带。Whisper 在页面按需下载。新用户在页面填写 API 凭据。默认端口 18000，被占用时选择 18000—18019 中的空闲端口，只监听 `127.0.0.1`。同目录已有实例就绪时打开它的页面；准备或退出期间提示等待。

控制台显示进度和错误，首次路径修复和检查全部成功后才原子写入成功标记。服务健康等待上限 120 秒；就绪检查结合持有的监听套接字、服务状态和系统端口所属进程，不打开其他服务。浏览器失败时显示手动地址并保留服务。失败返回非零状态，命令窗口保留错误。

按一次 Ctrl+C 正常停机，等待 Worker、检查点和下载子进程清理。Windows 若在服务完成退出后询问是否结束批处理，输入 Y 确认。关闭浏览器不会停止后台服务。便携启动器采用 Windows Selector 事件循环，以避免 Proactor 循环下遇到的停机挂起。

升级时先正常停机，备份 `data/`、`log/`；新目录完整解压新版后迁移资料、日志和可选 Whisper 模型。不要覆盖新版 `runtime/`、工具或默认权重。凭据仍沿用 Windows DPAPI，同机同账户恢复；换电脑或账户后重新填写。详见 [存储与恢复](storage.md) 和包内 `使用说明.txt`。

## 构建

需要 Windows x64、64 位 PowerShell、Conda、Node/npm、Git、网络和已验证的[自建 FFmpeg 构建目录](ffmpeg-build.md)。从仓库根目录执行。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts/build-portable.ps1 -Version 1.0.0 `
  -FFmpegBuild 'dist/ffmpeg-minimal/<构建编号>'
```

不在 PATH 的 Conda 可用 `-CondaExe <完整路径>` 指定。Python 固定 3.12.15，构建工具使用独立 venv 中的 `conda-pack==0.9.2`（[版本来源](https://pypi.org/project/conda-pack/)）。基础运行库从 conda-forge 安装，确切版本、构建、来源记录在 `licenses/conda-explicit.txt` 和清单中。后续可传 `-CondaExplicit <此前的 conda-explicit.txt>` 重用该层精确版本；后端与前端继续按仓库锁文件安装。

构建缓存和临时环境均位于本次独有的 `dist/.build/<编号>/`，不注册到现有 Conda 环境列表，不修改用户配置。工作目录和日志保留供定位失败；脚本不进行递归清理。同名 ZIP 或校验文件存在时停止，不覆盖旧产物。

收集规则是正向清单：后端仅 Git 跟踪的 `backend/app/**/*.py`；前端仅 Git 跟踪的构建配置、`src/` 和 `public/`，临时安装并生产构建。开发目录增加其他文件不会改变收集结果；不递归扫描其他本地目录。版本和未提交状态也随清单记录，未提交的新增构建输入记录摘要。

打包脚本核对指定 FFmpeg 构建的源码提交、补丁和归档 SHA-256，将 FFmpeg、FFprobe、配套 DLL 和三项组件的许可证收进便携包。对应的 `ffmpeg-sources.zip` 保存在自建构建目录，归档哈希见 `release-manifest.json`。Paraformer 收集目录定义的两个文件并沿用现有模型和 tokens 校验。默认 OCR 直接读取 RapidOCR wheel 内的权重。

```text
StudyBuddy-Windows-x64/
  启动.cmd、launcher.py、使用说明.txt
  LICENSE、THIRD_PARTY_NOTICES.txt、release-manifest.json
  runtime/、backend/app/、web/、tools/、models/、licenses/
  docs/SOURCE.md
```

ZIP 排除 `.env`、用户 `data/`、`log/`、项目测试、报告、实验、档案、编辑器设置、Git 目录、下载缓存和 `.pdb` 调试符号。运行库自身的测试模块和库内资源可能随第三方依赖保留。

运行环境通过 conda-pack 打包后只解压入暂存目录，不执行路径修复。所有验证在原始 ZIP 的独立副本中进行。清单记录包内文件 SHA-256、尺寸、依赖、模型、工具与来源；清单不递归包含自身摘要，ZIP 外部 SHA-256 覆盖整个包。输出为：

```text
dist/StudyBuddy-v1.0.0-windows-x64-preview.zip
dist/StudyBuddy-v1.0.0-windows-x64-preview.zip.sha256
```

第三方说明位于包内 `THIRD_PARTY_NOTICES.txt`，原始许可证、版权和模型声明位于 `licenses/`。依赖版本、来源和文件信息见 `release-manifest.json`。对应源码获取方式见随包的[源码说明](SOURCE.md)。[GitHub Releases](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases) 要求每个附件小于 2 GiB。

## 验证与记录

启动器和构建边界测试不依赖已有后端环境：

```powershell
python -B -m unittest discover -s scripts/portable -p test_portable.py -v
```

运行本机验收前，先核对 ZIP 的 SHA-256，再完整解压到独立的可写目录。使用未经初始化的副本，在仓库根目录执行以下命令。[验收工具](../scripts/portable/validate.py)

```powershell
& '<独立解压目录>/runtime/python.exe' -I -B -X utf8 scripts/portable/validate.py `
  --package '<独立解压目录>' --output '<包外报告目录>' --repo . --speech '<中文语音.wav>'
```

语音样本需含“学习”“三角”或“数学”之一，图片测试使用 Windows 微软雅黑。工具核对清单，检查首次准备失败与重试、重复启动、端口回退、Ctrl+C、设置恢复及本地处理。素材和 `acceptance.json` 写入 `--output` 指定的目录，验收范围为本机运行与本地处理。

仅做启动验收时，用 `--smoke-only` 替代 `--speech`。该模式仍检查清单、初始化、重复启动、端口回退、设置恢复及 Ctrl+C 退出，但跳过语音、OCR、媒体与文档处理，并在报告记录未执行项目。完整模式继续要求 `--speech`。GitHub 自动打包及草稿发布见 [CD 操作说明](cd.md)。
