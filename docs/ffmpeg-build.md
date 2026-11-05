# 构建 Windows x64 精简 FFmpeg

本指南用于构建 StudyBuddy 便携包中的 CPU 版 FFmpeg 和 FFprobe。x264 提供 H.264 编码，dav1d 提供 AV1 软件解码，VP9 和 Opus 等输入由 FFmpeg 原生解码器处理。

## 准备环境

- Windows x64 和 PowerShell。
- 64 位 Windows 原生 Python 3.12 或更新版，以及 Git。终端需能调用 `python` 和 `git`。
- Visual Studio 或 Build Tools，安装 MSVC 14.44 的 x64/x86 C++ 工具与 Windows SDK。
- 网络连接，用于下载源码和构建工具。脚本会在项目的 `dist/.ffmpeg-tools/` 中准备 MSYS2、Make、NASM、pkgconf、Meson 和 Ninja。

## 构建

在 StudyBuddy 仓库根目录执行命令。构建使用 `tools/ffmpeg/bin/ffmpeg.exe` 生成样例文件。若该工具尚未准备，先运行项目的安装脚本。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install-ffmpeg.ps1
```

随后运行构建脚本。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-ffmpeg.ps1
```

脚本按固定提交获取 FFmpeg 7.1 系列、x264 和 dav1d，编译 x264 与 dav1d 静态库，再链接进 FFmpeg 共享库。[源码清单](../scripts/ffmpeg/sources.json) · [编译脚本](../scripts/ffmpeg/build.sh)

## 使用产物

终端会显示输出目录 `dist/ffmpeg-minimal/<构建编号>/`。脚本完成自动检查后生成 ZIP。编译失败时，查看该目录中的 `build.log`。

- 独立使用时，运行 `ffmpeg-win64/bin/` 下的 `ffmpeg.exe` 或 `ffprobe.exe`。也可完整解压 `ffmpeg-win64.zip` 到其他目录，保持程序与配套 DLL 同在 `bin/`。头文件和链接库分别在成品的 `include/` 和 `lib/` 中。
- 构建便携包时，将整个输出目录传给 `scripts/build-portable.ps1` 的 `-FFmpegBuild` 参数。打包脚本需要同目录的 `ffmpeg-win64.zip`、`ffmpeg-sources.zip`、`build-manifest.json` 和 `SHA256SUMS.txt`。
- `ffmpeg-sources.zip` 包含三个组件的对应源码、构建脚本、配置与补丁。该组合按 GPLv3 构建，各组件保留原有许可。
