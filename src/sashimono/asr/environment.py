"""字幕起こしの実行環境

導入の仕組みそのものは :mod:`sashimono.runtime` にある ここは「字幕起こしに何が
要るか」だけを定義する AI 連携も同じ仕組みに載っているので、導入の画面と手順は
どちらも共通になる

CTranslate2 は cuDNN と cuBLAS の DLL を実行時に探す これが無いと CUDA を指定した
瞬間に落ちるが、無くても CPU では動く だから追加扱い（:attr:`FeaturePack.extra`）に
してある 合計で 1.7 GB あるので、要らない人が落とさずに済むことには意味がある
"""

from __future__ import annotations

import os
from pathlib import Path

from sashimono.runtime import FeaturePack, PackStatus, activate_runtime, install_runtime
from sashimono.runtime import install_command as _install_command

__all__ = [
    "ASR_PACK",
    "CUDA_PACKAGES",
    "REQUIRED_PACKAGES",
    "PackStatus",
    "activate_runtime",
    "install_command",
    "install_runtime",
    "model_cache_dir",
    "runtime_status",
]

#: 起こしそのものに要るもの
REQUIRED_PACKAGES: tuple[str, ...] = ("faster-whisper>=1.1",)

#: GPU で動かすために要るもの
CUDA_PACKAGES: tuple[str, ...] = ("nvidia-cublas-cu12", "nvidia-cudnn-cu12>=9.1")

ASR_PACK = FeaturePack(
    key="asr",
    label="字幕起こし",
    required=REQUIRED_PACKAGES,
    extra=CUDA_PACKAGES,
    extra_label="CUDA ランタイム",
    size_mb=300,
    extra_size_mb=1700,
)


def runtime_status() -> PackStatus:
    """いま何が入っているかを調べる

    パッケージを import せずに配布メタデータだけを見る faster-whisper の import は
    数秒かかるうえ、CUDA の DLL 探索まで走るので、状態確認のためにやってよい重さでは
    ない
    """
    return ASR_PACK.status()


def install_command(*, cuda: bool = True, upgrade: bool = False) -> list[str]:
    """導入に使う ``pip`` のコマンド列"""
    return _install_command(ASR_PACK, extra=cuda, upgrade=upgrade)


def model_cache_dir() -> Path:
    """モデルの取得先

    Hugging Face の既定に合わせる ここを独自の場所にすると、他のツールで
    落とし済みのモデルを二重に持つことになる
    """
    override = os.environ.get("HF_HOME")
    if override:
        return Path(override) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"
