# 同梱している部品と使用許諾（Third-party notices）

Kumiki 本体のソースコードは MIT License です（`LICENSE`）

配る zip（`Kumiki-<版>-windows-x64.zip`）には、Kumiki を動かすための他の部品を
一緒に入れてあります その中に GPL の部品（x264・x265）があるため、**zip は全体として
GPL の条件で配ります** Kumiki 本体の MIT は GPL と両立するので、本体のソースは MIT の
ままです 部品ごとの使用許諾はそれぞれの部品に従います

## zip の中の置き場

| 置き場 | 中身 |
|---|---|
| `LICENSE.txt` | Kumiki 本体の使用許諾（MIT） |
| `THIRD_PARTY_NOTICES.txt` | この一覧 |
| `licenses\GPL-2.0.txt` `GPL-3.0.txt` `LGPL-2.1.txt` `LGPL-3.0.txt` | GNU の使用許諾の全文 |
| `licenses\<包みの名前>-<版>\` | Python の包みが自分の dist-info に持っている使用許諾の写し 組み立てのたびに集める |
| `licenses\Python\LICENSE.txt` | Python 本体の使用許諾（同梱の OpenSSL・libffi・bzip2 などの分を含む） |
| `_internal\OpenGL\DLLS\` | freeglut と GLE の使用許諾（PyOpenGL が DLL と一緒に置いている物） |

## 部品の一覧

使用許諾の欄は、各部品の METADATA や同梱の使用許諾ファイルに書かれている表記です
※ の付いた物は、元の wheel に使用許諾の写しが入っておらず、上流の配布元の表記を書いています

### Python の包み

表の 1 列目は配布名（`tools/build_package.py` が組み立ての記録から数えた物と突き合わせる）

| 配布名 | 版 | 使用許諾 | ソース |
|---|---|---|---|
| `PySide6_Essentials` | 6.11.2 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only（Kumiki は LGPL-3.0 で使う） | https://code.qt.io/cgit/pyside/pyside-setup.git/ |
| `PySide6_Addons` | 6.11.2 | 同上 | 同上 |
| `shiboken6` | 6.11.2 | 同上 | 同上 |
| `av`（PyAV） | 18.1.0 | BSD-3-Clause | https://github.com/PyAV-Org/PyAV |
| `numpy` | 2.5.3 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0（同梱の OpenBLAS・LAPACK・GCC 実行時ライブラリの分は numpy の `LICENSE.txt` にある） | https://github.com/numpy/numpy |
| `lupa` | 2.8 | MIT（Lua の使用許諾を含む） | https://github.com/scoder/lupa |
| `PyOpenGL` | 3.1.10 | BSD License（分類子の表記 wheel に使用許諾の写しが無い） | https://github.com/mcfletch/pyopengl |
| `sounddevice` | 0.5.6 | MIT | https://github.com/spatialaudio/python-sounddevice |
| `cffi` | 2.1.1 | MIT-0 | https://github.com/python-cffi/cffi |
| `cryptography` | 50.0.1 | Apache-2.0 OR BSD-3-Clause | https://github.com/pyca/cryptography |
| `attrs` | 26.1.0 | MIT | https://github.com/python-attrs/attrs |
| `pip` | 26.2.1 | MIT（中に同梱の包みの分を含む） | https://github.com/pypa/pip |
| `setuptools` | 84.0.0 | MIT | https://github.com/pypa/setuptools |
| `packaging` | 26.3 | Apache-2.0 OR BSD-2-Clause | https://github.com/pypa/packaging |
| `trove-classifiers` | 2026.6.1.19 | Apache Software License | https://github.com/pypa/trove-classifiers |
| `typing_extensions` | 4.16.0 | PSF-2.0 | https://github.com/python/typing_extensions |
| `pywin32` | 312 | PSF | https://github.com/mhammond/pywin32 |
| `pyinstaller` | 6.22.3 | GPL-2.0 に配布物への例外付き（`Kumiki.exe` の起動部と実行時の差し込み） | https://github.com/pyinstaller/pyinstaller |

版は開発機の組み立てで数えた物です 組み立てる機械の包みが変わると版も変わり、
写しはそのとき入っている物から集め直します

### Python の包みの中の DLL

| 部品 | 置き場 | 使用許諾 | ソース |
|---|---|---|---|
| Qt 6（`Qt6*.dll` とプラグイン） | `_internal\PySide6\` | LGPL-3.0（PySide6 の表記に従う） | https://download.qt.io/official_releases/qt/ |
| FFmpeg 8.1.2（`avcodec` `avformat` `avutil` `avfilter` `avdevice` `swscale` `swresample`） | `_internal\av.libs\` | 組み込まれた FFmpeg が返す表記は「LGPL version 3 or later」（`--enable-version3`） | https://ffmpeg.org/download.html 組み立て方は https://github.com/PyAV-Org/pyav-ffmpeg |
| x264（`libx264-165`） ※ | `_internal\av.libs\` | GPL-2.0-or-later | https://code.videolan.org/videolan/x264 |
| x265 ※ | `_internal\av.libs\` | GPL-2.0-or-later | https://bitbucket.org/multicoreware/x265_git |
| dav1d ※ | `_internal\av.libs\` | BSD-2-Clause | https://code.videolan.org/videolan/dav1d |
| LAME（`libmp3lame`） ※ | `_internal\av.libs\` | LGPL | https://lame.sourceforge.io/ |
| opencore-amr（amrnb / amrwb） ※ | `_internal\av.libs\` | Apache-2.0 | https://sourceforge.net/projects/opencore-amr/ |
| Opus ※ | `_internal\av.libs\` | BSD-3-Clause | https://opus-codec.org/ |
| SVT-AV1 ※ | `_internal\av.libs\` | BSD-3-Clause-Clear と AOM Patent License 1.0 | https://gitlab.com/AOMediaCodec/SVT-AV1 |
| libvpx ※ | `_internal\av.libs\` | BSD-3-Clause | https://chromium.googlesource.com/webm/libvpx |
| libwebp（`libwebp` `libwebpmux` `libsharpyuv`） ※ | `_internal\av.libs\` | BSD-3-Clause | https://chromium.googlesource.com/webm/libwebp |
| libvpl ※ | `_internal\av.libs\` | MIT | https://github.com/intel/libvpl |
| libiconv ※ | `_internal\av.libs\` | LGPL-2.1-or-later | https://www.gnu.org/software/libiconv/ |
| zlib ※ | `_internal\av.libs\` | Zlib | https://zlib.net/ |
| GCC 実行時ライブラリ（`libgcc_s_seh` `libstdc++`） ※ | `_internal\av.libs\` | GPL-3.0-or-later WITH GCC-exception-3.1 | https://gcc.gnu.org/ |
| winpthreads（`libwinpthread`） ※ | `_internal\av.libs\` | MinGW-w64 の winpthreads の COPYING に従う | https://github.com/mingw-w64/mingw-w64 |
| OpenBLAS | `_internal\numpy.libs\` | BSD-3-Clause（numpy の `LICENSE.txt`） | https://github.com/OpenMathLib/OpenBLAS |
| Lua 5.1〜5.5・LuaJIT 2.0 / 2.1 | `_internal\lupa\` | MIT（Lua は lupa の `LICENSE.txt` LuaJIT は ※） | https://www.lua.org/ https://luajit.org/ |
| PortAudio | `_internal\_sounddevice_data\` | MIT（`*-asio.dll` は Steinberg の ASIO SDK を含む） | https://github.com/spatialaudio/portaudio-binaries |
| freeglut・GLE | `_internal\OpenGL\DLLS\` | 同じフォルダの `freeglut_COPYING.txt` `gle_COPYING` | https://github.com/mcfletch/pyopengl |
| Python 3.14 と同梱の OpenSSL・libffi など | `_internal\` | PSF-2.0 ほか（`licenses\Python\LICENSE.txt`） | https://www.python.org/downloads/source/ |
| Microsoft Visual C++ 再頒布可能パッケージ（`VCRUNTIME140*.dll` `MSVCP140*.dll`） | `_internal\` ほか | Microsoft の再頒布の条件（`licenses\Python\LICENSE.txt` の「Additional Conditions for this Windows binary build」） | なし |

## ソースの入手先

GPL と LGPL の部品は、上の表の「ソース」の欄から対応するソースを入手できます
FFmpeg とそれに組み込まれた x264・x265 などは、PyAV の wheel のために
https://github.com/PyAV-Org/pyav-ffmpeg が組み立てた物です 組み立てに使った FFmpeg の
設定は次で確かめられます（Kumiki の開発環境で）

```
.venv\Scripts\python.exe -c "import av._core as c; print(c.library_meta['libavcodec'])"
```

Kumiki 本体のソースは https://github.com/kagemorikosame/kumiki にあります

## Qt を差し替える

Qt は LGPL-3.0 で使っています zip は 1 つの exe へまとめる形ではなく、フォルダの形
（PyInstaller の onedir）で組み立てているので、Qt の DLL（`_internal\PySide6\Qt6*.dll`）と
PySide6 の部品は別のファイルのまま置かれています 使う人は、互換のある版の Qt や
PySide6 に差し替えて動かせます
