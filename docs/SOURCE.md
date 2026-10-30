# 对应源码与第三方组件来源

本文列出 StudyBuddy 项目源码、已核对的第三方组件版本，以及源码、构建脚本和补丁的获取方式。
具体便携包的版本、源码提交、依赖和文件 SHA-256 见包根目录的 `release-manifest.json`。

## 本项目源码与许可

StudyBuddy 自有代码采用 **GPL-3.0-only**，许可证全文见包根目录的 `LICENSE`。
第三方软件、库、前端组件及模型权重保留各自许可；原始版权、许可证和适用的 NOTICE
见包内 `licenses/` 和 `THIRD_PARTY_NOTICES.txt`。

- 项目仓库：[NEKOSAMA514/StudyBuddy](https://github.com/NEKOSAMA514/StudyBuddy)。
- 版本发布：[Releases](https://github.com/NEKOSAMA514/StudyBuddy/releases)。
- 本项目对应源码：下载本便携包所在 Release、同一 tag 下的 **Source code (zip)** 或
  **Source code (tar.gz)**。不要用其他版本或最新分支代替。
- 精确提交：查看 `release-manifest.json` 的 `source.commit`，在项目仓库中打开该提交，
  即可取得该提交的代码。源码包括后端、前端、依赖锁文件、打包脚本和本项目维护的构建补丁。

GitHub 自动生成的源码归档提供本仓库的文件；下表所列第三方源码需通过各自入口获取。
发布二进制所用的源码和构建修改应包含在对应提交中，未提交改动不能由同版本 tag 自动补足。

## 独立 FFmpeg：自建精简 Windows x64 构建

本节对应 `scripts/ffmpeg/sources.json` 固定的精简构建，外部媒体库为 x264、dav1d。
它与下节原装 PyAV 内置的 FFmpeg 是两套独立构建，源码版本分别列出。

| 组件 | 精确版本或提交 | 源码获取 |
| --- | --- | --- |
| FFmpeg 7.1 系列 | `1fdbca85aaea513c9cc6c14d347f76543346d3da` | [源码快照](https://codeload.github.com/FFmpeg/FFmpeg/tar.gz/1fdbca85aaea513c9cc6c14d347f76543346d3da) |
| x264 | `0480cb05fa188d37ae87e8f4fd8f1aea3711f7ee` | [源码快照](https://code.videolan.org/videolan/x264/-/archive/0480cb05fa188d37ae87e8f4fd8f1aea3711f7ee/x264-0480cb05fa188d37ae87e8f4fd8f1aea3711f7ee.tar.bz2) |
| dav1d | `c150ba6c9b9be0956330a9ddfee33ad88f2b1bc5` | [源码快照](https://code.videolan.org/videolan/dav1d/-/archive/c150ba6c9b9be0956330a9ddfee33ad88f2b1bc5/dav1d-c150ba6c9b9be0956330a9ddfee33ad88f2b1bc5.tar.bz2) |

从本项目对应版本源码中取得以下构建材料，路径相对于项目仓库根目录：

- `scripts/build-ffmpeg.ps1`：构建入口。
- `scripts/ffmpeg/build.py`、`scripts/ffmpeg/build.sh`：工具准备、编译、安装及归档步骤。
- `scripts/ffmpeg/sources.json`：固定源码提交和 Git 树摘要。
- `scripts/ffmpeg/patches/ffmpeg-mswindres-utf8.patch`：Windows 资源编译使用 UTF-8 的补丁。
- `scripts/ffmpeg/verify.py`：该构建的验证步骤。
- `docs/ffmpeg-build.md`：构建说明。

构建产物中的 `build-manifest.json` 记录实际编译配置、工具版本和归档哈希。
生成的 `ffmpeg-sources.zip` 包含三份对应源码及本次构建材料。
此索引不适用于此前使用的 Gyan FFmpeg 9.0.2 成品。

## 原装 PyAV 15.1.0 及内置 FFmpeg

适用对象为原装 Windows x64、CPython 3.12 的 PyAV 15.1.0 wheel。
其 vendor 清单选择 `pyav-ffmpeg` 的 `7.1.1-6`，固定仓库提交为
`f665a9654da201e8d5c9ec2b7597ae90fbdedba5`。

| 组件 | 精确版本或提交 | 源码获取 |
| --- | --- | --- |
| PyAV | 15.1.0 | [源码发行包](https://files.pythonhosted.org/packages/e9/c3/83e6e73d1592bc54436eae0bc61704ae0cff0c3cfbde7b58af9ed67ebb49/av-15.1.0.tar.gz)、[含工作流和构建脚本的仓库快照](https://github.com/PyAV-Org/PyAV/archive/refs/tags/v15.1.0.tar.gz) |
| FFmpeg | 7.1.1 | [源码包](https://ffmpeg.org/releases/ffmpeg-7.1.1.tar.xz) |
| x264 | `32c3b801191522961102d4bea292cdb61068d0dd`，core 165 | [源码快照](https://code.videolan.org/videolan/x264/-/archive/32c3b801191522961102d4bea292cdb61068d0dd/x264-32c3b801191522961102d4bea292cdb61068d0dd.tar.bz2) |
| x265 | 4.1，DLL 自报 `4.1+1-1d117be` | [源码包](https://bitbucket.org/multicoreware/x265_git/downloads/x265_4.1.tar.gz) |
| libaom | 3.11.0 | [源码包](https://storage.googleapis.com/aom-releases/libaom-3.11.0.tar.gz) |
| dav1d | 1.5.1 | [源码快照](https://code.videolan.org/videolan/dav1d/-/archive/1.5.1/dav1d-1.5.1.tar.bz2) |
| SVT-AV1 | 3.0.1 | [源码快照](https://gitlab.com/AOMediaCodec/SVT-AV1/-/archive/v3.0.1/SVT-AV1-v3.0.1.tar.bz2) |
| LAME | 3.100 | [源码包](https://deb.debian.org/debian/pool/main/l/lame/lame_3.100.orig.tar.gz) |
| TwoLAME | 0.4.0 | [源码包](https://deb.debian.org/debian/pool/main/t/twolame/twolame_0.4.0.orig.tar.gz) |
| libogg | 1.3.5 | [源码包](https://downloads.xiph.org/releases/ogg/libogg-1.3.5.tar.gz) |
| libvorbis | 1.3.7 | [源码包](https://ftp.osuosl.org/pub/xiph/releases/vorbis/libvorbis-1.3.7.tar.xz) |
| Opus | 1.5.2 | [源码包](https://github.com/xiph/opus/releases/download/v1.5.2/opus-1.5.2.tar.gz) |
| Speex | 1.2.1 | [源码包](https://downloads.xiph.org/releases/speex/speex-1.2.1.tar.gz) |
| libvpx | 1.15.1 | [源码快照](https://github.com/webmproject/libvpx/archive/v1.15.1.tar.gz) |
| libwebp，包括 webpmux、sharpyuv | 1.5.0 | [源码快照](https://github.com/webmproject/libwebp/archive/refs/tags/v1.5.0.tar.gz) |
| OpenH264 | 2.6.0 | [源码快照](https://github.com/cisco/openh264/archive/refs/tags/v2.6.0.tar.gz) |
| opencore-amr，包括 NB、WB | 0.1.5 | [源码包](https://deb.debian.org/debian/pool/main/o/opencore-amr/opencore-amr_0.1.5.orig.tar.gz) |
| GMP | 6.3.0 | [源码包](https://ftp.gnu.org/gnu/gmp/gmp-6.3.0.tar.xz) |
| SRT | 1.5.4 | [源码快照](https://github.com/Haivision/srt/archive/refs/tags/v1.5.4.tar.gz) |

FFmpeg 7.1.1 源码覆盖以下七个 FFmpeg DLL：`avcodec`、`avformat`、`avutil`、
`avdevice`、`avfilter`、`swscale`、`swresample`。

构建材料与校验值：

- [pyav-ffmpeg 固定仓库源码包](https://github.com/PyAV-Org/pyav-ffmpeg/archive/f665a9654da201e8d5c9ec2b7597ae90fbdedba5.tar.gz)。
- [固定构建配方](https://github.com/PyAV-Org/pyav-ffmpeg/blob/f665a9654da201e8d5c9ec2b7597ae90fbdedba5/scripts/build-ffmpeg.py)：源码 URL、SHA-256、Windows 编译选项及库选择。
- 配套构建实现为同一仓库中的 `scripts/cibuildpkg.py`，工作流为 `.github/workflows/build-ffmpeg.yml`。
- [固定补丁目录](https://github.com/PyAV-Org/pyav-ffmpeg/tree/f665a9654da201e8d5c9ec2b7597ae90fbdedba5/patches)：`ffmpeg.patch`、`gmp.patch`、`lame.patch`、`twolame.patch`、`vorbis.patch`、`vpx.patch`，均包含在上述仓库快照中。

原装构建链接了 x264/x265；其中 FFmpeg 的 configure 检查被补丁修改。
其运行时 LGPL 字符串不能代替对这些 GPL 组件的许可处理，第三方原始许可证仍须保留。

### 随包基础库与 GCC 运行库

| 组件及 DLL 简称 | 已确认版本 | 源码获取 |
| --- | --- | --- |
| OpenSSL，`libcrypto-1_1-x64` | 1.1.1w | [上游源码包](https://github.com/openssl/openssl/releases/download/OpenSSL_1_1_1w/openssl-1.1.1w.tar.gz) |
| GNU libiconv，`libiconv-2` | 1.18 | [上游源码包](https://ftp.gnu.org/gnu/libiconv/libiconv-1.18.tar.gz) |
| zlib，`zlib1` | 1.3.1 | [上游源码包](https://zlib.net/fossils/zlib-1.3.1.tar.gz) |
| GCC，`libgcc_s_seh-1`、`libstdc++-6` | MSYS2 MINGW64 `gcc-libs 15.1.0-5` | [含 GCC 源码、MSYS2 补丁和配方的源码包](https://repo.msys2.org/mingw/sources/mingw-w64-gcc-15.1.0-5.src.tar.zst) |
| MinGW-w64 winpthreads，`libwinpthread-1` | DLL 自报 1.0.0.0；源码修订尚未锁定 | [项目源码入口](https://github.com/mingw-w64/mingw-w64/tree/master/mingw-w64-libraries/winpthreads)、[许可文本参考入口](https://github.com/mingw-w64/mingw-w64/blob/master/mingw-w64-libraries/winpthreads/COPYING) |

GCC 运行库源码包已与原装 wheel 的两份 DLL 核对，SHA-256 为
`9de05f20e55c10fcd3f06c028614083b3c45f9b53714f06f32d60553a07262e5`。
其 `PKGBUILD` 对应 MSYS2 仓库提交
[`9fa882f7eb6f639780a13df016497a93e45544ac`](https://github.com/msys2/MINGW-packages/tree/9fa882f7eb6f639780a13df016497a93e45544ac/mingw-w64-gcc)。
随包声明应保留 GPL、GCC Runtime Library Exception 及适用的原始版权文本。

来源核对说明：OpenSSL、libiconv、zlib 已确认到表中的上游版本，原始供应商打包修订和额外补丁尚未完整核对。
winpthreads 的两个链接是项目及许可参考入口，不是已锁定的对应源码快照；不能把其 DLL 版本号当作 MinGW-w64 的源码版本。

## Shapely 与 GEOS

| 组件 | 版本 | 源码获取 |
| --- | --- | --- |
| Shapely | 2.2.0 | [源码发行包](https://files.pythonhosted.org/packages/f3/ab/924b6e202f796d270a3041a230151f7908db5ea48c74effe6f8023e9bd05/shapely-2.2.0.tar.gz)、[含构建工作流的仓库快照](https://github.com/shapely/shapely/archive/refs/tags/2.2.0.tar.gz) |
| GEOS | 3.14.1 | [源码包](https://download.osgeo.org/geos/geos-3.14.1.tar.bz2) |

Shapely 的构建脚本和工作流从上述同版本仓库快照取得，GEOS 的 CMake 构建文件在 GEOS 源码包内。
GEOS 原始许可文本随其源码保留，包内还应保留对应的第三方许可材料。

## 已记录的其他组件来源

以下是此前补录许可材料的组件入口。其原始许可与版权声明仍随包保留；本表不以项目顶层许可代替第三方许可。

| 组件 | 版本 | 源码获取 |
| --- | --- | --- |
| CTranslate2 | 4.8.2 | [源码快照](https://github.com/OpenNMT/CTranslate2/archive/refs/tags/v4.8.2.tar.gz) |
| FlatBuffers | 25.12.19 | [源码快照](https://github.com/google/flatbuffers/archive/refs/tags/v25.12.19.tar.gz) |
| sherpa-onnx-core | 1.13.8 | [sherpa-onnx 源码快照](https://github.com/k2-fsa/sherpa-onnx/archive/refs/tags/v1.13.8.tar.gz) |
| tokenizers | 0.23.3 | [源码快照](https://github.com/huggingface/tokenizers/archive/refs/tags/v0.23.3.tar.gz) |
| rehype-katex | 7.0.1 | [npm 包，含源码与许可](https://registry.npmjs.org/rehype-katex/-/rehype-katex-7.0.1.tgz) |
| remark-math | 6.0.0 | [npm 包，含源码与许可](https://registry.npmjs.org/remark-math/-/remark-math-6.0.0.tgz) |

其他 Python、Conda、前端依赖及模型的准确版本和原始来源见 `release-manifest.json`。
Conda 精确包记录见 `licenses/conda-explicit.txt`，对应配方和许可见 `licenses/conda/`；
Python、前端和模型许可分别见 `licenses/python/`、`licenses/frontend/`、`licenses/models/`。
构建所用应用依赖锁文件为项目源码中的 `backend/requirements.lock` 和 `frontend/package-lock.json`。

## 获取与版本核对

- 采用表中固定版本、提交和构建配方，不使用最新分支或相近版本替代。
- 上游配方给出的 SHA-256 用于核对相应下载包；同一源码的不同压缩格式不能直接套用该值。
- 同一项目生成的多个 DLL 可共用其对应源码包；源码包还需配套取得本次构建实际使用的补丁和脚本。
- 依赖、二进制构建或补丁变化时，同步更新此索引；项目自身源码版本按每个便携包的清单定位。
- 链接失效或无法定位对应版本时，请在 [StudyBuddy Issues](https://github.com/NEKOSAMA514/StudyBuddy/issues)
  附上便携包名称、版本、失效链接和 `release-manifest.json`，以便核对并恢复相应源码获取入口。

本文件是源码获取索引，不表示所有链接指向的材料都已下载归档，或尚未核对的供应商构建已完成验证。
