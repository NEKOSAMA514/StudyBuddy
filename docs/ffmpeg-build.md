# 精简 Windows x64 FFmpeg

此构建只交付独立 FFmpeg 和开发文件，不安装到现有应用，不构建 PyAV。
源码固定为 FFmpeg 7.1 系列、x264、dav1d，具体提交见
[源码清单](../scripts/ffmpeg/sources.json)。x264 提供 H.264 编码，dav1d 提供 AV1 软件解码；
VP9、Opus 等读取能力使用 FFmpeg 原生实现。

## 构建

需要 Windows x64、原生 Python 3.12+、Git、MSVC 14.44 和 Windows SDK。入口会在
`dist/.ffmpeg-tools/` 准备隔离的 MSYS2、Make、NASM、pkgconf、Meson 1.12.1 和 Ninja 1.13.2。
这些编译工具不进入成品包，也不修改全局 PATH。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-ffmpeg.ps1
```

[构建入口](../scripts/build-ffmpeg.ps1)按固定提交准备独立源码工作副本，校验 Git 提交和树摘要。
先构建使用静态 MSVC 运行库的 x264、dav1d 静态库，再构建 FFmpeg 自身 DLL、ffmpeg 和 ffprobe。
关闭外部库自动探测，保留原生组件；启用 Windows 原生线程和 Schannel，不要求 GPU 或本机专属 CPU 指令。

工作文件位于 `dist/.ffmpeg-build/<编号>/`，输出位于 `dist/ffmpeg-minimal/<编号>/`。
失败保留日志与缓存，并在 `build-manifest.json` 中记录失败；重试生成新的构建目录。

## 交付内容

- `ffmpeg-win64/`：`bin/` 中的程序和 DLL，`include/`、`lib/` 中的开发文件，以及许可证和构建记录。
- `ffmpeg-win64.zip`：上述独立成品。
- `ffmpeg-sources.zip`：三份对应源码、本次构建脚本、配置与记录。
- `SHA256SUMS.txt`：成品、源码包和构建清单的 SHA-256。
- `build.log`、`validation/report.json`：编译日志和真实媒体验证结果。

EXE 与 DLL 应保持在同一 `bin/` 目录。开发文件为下一轮编译 PyAV 准备，不表示本轮已经验证 PyAV。
源码包保留各组件原有许可；此组合按 GPLv3 构建。

## 验证范围

测试素材由现有完整 FFmpeg 生成，再交给新成品处理；测试使用的编码器不会因此加入新成品。
覆盖 H.264、HEVC、VP9、AV1 和现有测试中的主要容器，验证解码、音频提取、JPEG 截图、
H.264/AAC 预览、无损封装与音画合并，以及无音轨、比例、非零时间起点、可变帧率和延迟音轨补静音。

另检查 x64 文件、共享库依赖、开发文件、编译配置，并在排除编译工具目录的 PATH 中运行成品。
这是本机验证，不代表已完成 Windows 10/11 干净系统验收，也不代表整个应用已完成许可审查。
