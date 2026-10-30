#!/usr/bin/env bash
set -e -o pipefail

work="$(cygpath -u "$STUDY_BUILD_WORK")"
prefix="$(cygpath -m "$STUDY_BUILD_PREFIX")"
python="$(cygpath -u "$STUDY_BUILD_PYTHON")"
tools="$(cygpath -u "$STUDY_BUILD_TOOLS")"
# Native compiler/linker precede MSYS utilities (which also contain a link.exe).
export PATH="$(cygpath -u "$VCTOOLSINSTALLDIR")/bin/Hostx64/x64:$tools/venv/Scripts:$PATH:/usr/bin"
export CC=cl.exe CXX=cl.exe
export PKG_CONFIG_PATH="$(cygpath -u "$STUDY_BUILD_PREFIX")/lib/pkgconfig"
export PKG_CONFIG_LIBDIR="$PKG_CONFIG_PATH"
jobs="$(getconf _NPROCESSORS_ONLN)"
(( jobs > 8 )) && jobs=8

echo '=== x264 ==='
cd "$work/src/x264"
./configure --prefix="$prefix" --enable-static --disable-cli --disable-opencl \
    --extra-cflags=-MT
make -j"$jobs"
make install

echo '=== dav1d ==='
"$python" -m mesonbuild.mesonmain setup "$(cygpath -m "$work/dav1d-build")" \
    "$(cygpath -m "$work/src/dav1d")" --prefix="$prefix" --libdir=lib \
    --buildtype=release --default-library=static -Db_vscrt=mt \
    -Denable_tools=false -Denable_tests=false --wrap-mode=nodownload
"$python" -m ninja -C "$(cygpath -m "$work/dav1d-build")"
"$python" -m ninja -C "$(cygpath -m "$work/dav1d-build")" install
# dav1d installs its MSVC COFF archive with a .a suffix; FFmpeg's MSVC linker expects dav1d.lib.
cp "$prefix/lib/libdav1d.a" "$prefix/lib/dav1d.lib"

echo '=== FFmpeg ==='
git -C "$work/src/ffmpeg" apply "$(dirname "$0")/patches/ffmpeg-mswindres-utf8.patch"
mkdir -p "$work/ffmpeg-build"
cd "$work/ffmpeg-build"
options=(
    --toolchain=msvc --arch=x86_64 --prefix="$prefix"
    --enable-shared --disable-static --disable-autodetect
    --enable-gpl --enable-version3 --enable-libx264 --enable-libdav1d
    --enable-w32threads --disable-pthreads --enable-schannel
    --disable-ffplay --disable-doc --disable-debug
    --pkg-config=pkg-config --pkg-config-flags=--static
    --extra-cflags=-MT --extra-version=studybuddy-1fdbca85aa
)
printf '%q ' "$work/src/ffmpeg/configure" "${options[@]}" > "$work/ffmpeg-configure.txt"
printf '\n' >> "$work/ffmpeg-configure.txt"
"$work/src/ffmpeg/configure" "${options[@]}"
make -j"$jobs"
make install
