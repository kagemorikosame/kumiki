"""音声の再生と、再生位置の時計

A/V 同期はオーディオを基準にする 映像を基準にすると、映像の遅れを取り戻すために
音を飛ばすことになり、途切れが即座に耳につく 音は途切れさせず、映像側が
追いつけない分をコマ落としで吸収する

そのため、このクラスは「音を出す」だけでなく「今どこを再生しているか」を答える
時計でもある
"""

from __future__ import annotations

import threading
from collections.abc import Callable

import numpy as np
import sounddevice as sd

from sashimono.engine.audio.mixer import AudioMixer

__all__ = ["AudioPlayer", "PlaybackError"]

#: 出力に一度に書き込むサンプル数 小さいほど遅延は減るが、書き込み回数が増える
BLOCK_SAMPLES = 1024

#: 出力デバイスに要求する遅延 小さくしすぎると音が途切れる
LATENCY = "low"


class PlaybackError(RuntimeError):
    """音声出力を開けない"""


class AudioPlayer:
    """ミキサの出力をサウンドデバイスへ流す

    再生は専用スレッドで行う デコードを含むミックスをオーディオコールバックの
    中で行うと、ディスクが詰まった瞬間に音が途切れる
    """

    def __init__(
        self,
        mixer: AudioMixer,
        *,
        on_finished: Callable[[], None] | None = None,
    ) -> None:
        self._mixer = mixer
        self._on_finished = on_finished
        self._stream: sd.OutputStream | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._position = 0
        self._written = 0
        self._start_sample = 0
        self._end_sample: int | None = None

    @property
    def is_playing(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def position_sample(self) -> int:
        """今スピーカーから出ているおおよそのサンプル位置

        デバイスのバッファに積んだ分だけ、書き込み位置は実際の再生位置より先を
        行っている その差を引いて返す
        """
        with self._lock:
            return self._position

    def start(self, from_sample: int, *, end_sample: int | None = None) -> None:
        """``from_sample`` から再生を始める すでに再生中なら一度止める"""
        self.stop()
        self._stop.clear()
        self._start_sample = max(0, from_sample)
        self._end_sample = end_sample
        with self._lock:
            self._position = self._start_sample
            self._written = 0

        try:
            self._stream = sd.OutputStream(
                samplerate=self._mixer.sample_rate,
                channels=self._mixer.channels,
                dtype="float32",
                blocksize=BLOCK_SAMPLES,
                latency=LATENCY,
            )
            self._stream.start()
        except sd.PortAudioError as exc:
            self._stream = None
            raise PlaybackError(f"音声出力を開けない: {exc}") from exc

        self._thread = threading.Thread(target=self._run, name="sashimono-audio", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """再生を止める 止まるまで待つ"""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._close_stream()

    def close(self) -> None:
        self.stop()

    def _run(self) -> None:
        stream = self._stream
        if stream is None:
            return

        cursor = self._start_sample
        try:
            while not self._stop.is_set():
                count = BLOCK_SAMPLES
                if self._end_sample is not None:
                    count = min(count, self._end_sample - cursor)
                    if count <= 0:
                        break

                block = self._mixer.render(cursor, count)
                # 出力段で 1 度だけ頭打ちにする 途中で潰すと、後段の調整で
                # 潰れた音しか扱えなくなる
                stream.write(np.clip(block, -1.0, 1.0))
                cursor += count

                with self._lock:
                    self._written += count
                    # 書き込んだ分から、まだデバイスのバッファに残っている分を引く
                    buffered = int(stream.latency * self._mixer.sample_rate)
                    self._position = max(
                        self._start_sample, self._start_sample + self._written - buffered
                    )
        except (sd.PortAudioError, RuntimeError):
            # デバイスが抜かれた等 再生を諦めるが、アプリは落とさない
            pass
        finally:
            if not self._stop.is_set() and self._on_finished is not None:
                self._on_finished()

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop()
            stream.close()
        except sd.PortAudioError:
            pass
