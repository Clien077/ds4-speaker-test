# -*- coding: utf-8 -*-
"""基于 sounddevice 的播放引擎、实时电平表与同步录音。"""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, List, Optional

import numpy as np
import sounddevice as sd

from dst_audio import map_channels, to_db


class LevelMeter:
    """线程安全的电平表：峰值 / RMS / 削波计数（回调线程写，界面线程读）。"""

    def __init__(self, channels: int = 2):
        self.channels = max(1, int(channels))
        self._lock = threading.Lock()
        self._peak = np.zeros(self.channels, dtype=np.float64)
        self._sumsq = np.zeros(self.channels, dtype=np.float64)
        self._frames = 0
        self._clipped = 0

    def reset(self) -> None:
        with self._lock:
            self._peak[:] = 0.0
            self._sumsq[:] = 0.0
            self._frames = 0
            self._clipped = 0

    def push(self, block: np.ndarray) -> None:
        x = np.asarray(block, dtype=np.float32)
        if x.ndim == 1:
            x = x[:, None]
        if x.size == 0:
            return
        peak = np.abs(x).max(axis=0).astype(np.float64)
        sumsq = (x.astype(np.float64) ** 2).sum(axis=0)
        clipped = int(np.count_nonzero(np.abs(x) >= 0.999))
        with self._lock:
            n = min(x.shape[1], self._peak.size)
            self._peak[:n] = np.maximum(self._peak[:n], peak[:n])
            self._sumsq[:n] += sumsq[:n]
            self._frames += int(x.shape[0])
            self._clipped += clipped

    def snapshot(self, do_reset: bool = True) -> Dict[str, object]:
        with self._lock:
            frames = max(self._frames, 1)
            rms = np.sqrt(self._sumsq / frames)
            out = {
                "peak": self._peak.copy(),
                "rms": rms,
                "peak_db": [to_db(v) for v in self._peak],
                "rms_db": [to_db(v) for v in rms],
                "clipped": self._clipped,
                "frames": self._frames,
            }
            if do_reset:
                self._peak[:] = 0.0
                self._sumsq[:] = 0.0
                self._frames = 0
                self._clipped = 0
            return out


def make_extra_settings(exclusive: bool):
    """WASAPI 独占模式设置（仅在 WASAPI 端点上有效）。"""
    if not exclusive:
        return None
    try:
        return sd.WasapiSettings(exclusive=True)
    except Exception:  # noqa: BLE001 - 非 Windows / 旧版 PortAudio
        return None


class Player:
    """把一段内存中的音频推送到指定输出设备，并实时统计电平。"""

    def __init__(
        self,
        device: int,
        samplerate: int,
        channels: int,
        volume: float = 1.0,
        loop: bool = False,
        exclusive: bool = False,
        blocksize: int = 0,
        latency: Optional[float] = None,
        on_finish: Optional[Callable[[], None]] = None,
    ):
        self.device = int(device)
        self.samplerate = int(samplerate)
        self.channels = max(1, int(channels))
        self.volume = float(volume)
        self.loop = bool(loop)
        self.exclusive = bool(exclusive)
        self.blocksize = int(blocksize)
        self.latency = latency
        self.on_finish = on_finish

        self.meter = LevelMeter(self.channels)
        self._data = np.zeros((0, self.channels), dtype=np.float32)
        self._pos = 0
        self._played = 0
        self._lock = threading.Lock()
        self._stop_flag = False
        self._finished = threading.Event()
        self._stream: Optional[sd.OutputStream] = None
        self.started_at: Optional[float] = None

    # ---------------------------------------------------------------- 状态
    @property
    def is_playing(self) -> bool:
        return self._stream is not None and not self._finished.is_set()

    @property
    def played_frames(self) -> int:
        return self._played

    @property
    def duration(self) -> float:
        n = self._data.shape[0]
        if not self.loop and n:
            return n / float(self.samplerate)
        return 0.0

    # ---------------------------------------------------------------- 控制
    def start(self, samples: np.ndarray) -> None:
        if self.is_playing:
            raise RuntimeError("播放器正在播放中")
        data = np.asarray(samples, dtype=np.float32)
        if data.ndim == 1:
            data = data[:, None]
        if data.shape[1] != self.channels:
            data = map_channels(data, self.channels)
        if data.shape[0] == 0:
            raise RuntimeError("没有可播放的音频数据")
        self._data = np.ascontiguousarray(data, dtype=np.float32)
        self._pos = 0
        self._played = 0
        self._stop_flag = False
        self._finished.clear()
        self.meter.reset()
        try:
            self._stream = sd.OutputStream(
                samplerate=self.samplerate,
                device=self.device,
                channels=self.channels,
                dtype="float32",
                blocksize=self.blocksize,
                latency=self.latency,
                callback=self._callback,
                finished_callback=self._on_stream_finished,
                extra_settings=make_extra_settings(self.exclusive),
            )
            self._stream.start()
        except Exception as exc:  # noqa: BLE001
            self._stream = None
            self._finished.set()
            hint = ""
            if self.exclusive:
                hint = "（独占模式要求采样率与设备原生格式一致，可尝试去掉独占或换 WASAPI 端点）"
            raise RuntimeError("无法打开输出设备 %d：%s%s" % (self.device, exc, hint)) from exc
        self.started_at = time.time()

    def stop(self) -> None:
        with self._lock:
            self._stop_flag = True
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.abort()
            except Exception:  # noqa: BLE001
                pass
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass
        self._finished.set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        """阻塞等待播放自然结束；返回 True 表示已结束。"""
        return self._finished.wait(timeout)

    # ---------------------------------------------------------------- 回调
    def _fill(self, out: np.ndarray, frames: int) -> int:
        """从游标处填充输出缓冲；返回实际写入的帧数（循环模式会回绕）。"""
        total = self._data.shape[0]
        if total == 0:
            return 0
        need, off = frames, 0
        while need > 0:
            if self._pos >= total:
                if self.loop:
                    self._pos = 0
                else:
                    break
            take = min(need, total - self._pos)
            out[off:off + take] = self._data[self._pos:self._pos + take]
            self._pos += take
            off += take
            need -= take
        self._played += off
        return off

    def _callback(self, outdata, frames, time_info, status) -> None:  # noqa: ANN001
        outdata[:] = 0.0
        with self._lock:
            if self._stop_flag:
                raise sd.CallbackStop
            written = self._fill(outdata, frames)
        if self.volume != 1.0:
            np.multiply(outdata, self.volume, out=outdata)
        np.clip(outdata, -1.0, 1.0, out=outdata)
        self.meter.push(outdata)
        if written < frames and not self.loop:
            raise sd.CallbackStop

    def _on_stream_finished(self) -> None:
        self._finished.set()
        if self.on_finish is not None:
            try:
                self.on_finish()
            except Exception:  # noqa: BLE001
                pass


class Recorder:
    """把输入设备的数据累积到内存（用于回环测试）。"""

    def __init__(
        self,
        device: int,
        samplerate: int,
        channels: int = 1,
        blocksize: int = 0,
        latency: Optional[float] = None,
        exclusive: bool = False,
    ):
        self.device = int(device)
        self.samplerate = int(samplerate)
        self.channels = max(1, int(channels))
        self.blocksize = int(blocksize)
        self.latency = latency
        self.exclusive = exclusive
        self.meter = LevelMeter(self.channels)
        self._blocks: List[np.ndarray] = []
        self._lock = threading.Lock()
        self._stream: Optional[sd.InputStream] = None
        self.overflow = 0

    @property
    def is_recording(self) -> bool:
        return self._stream is not None

    @property
    def recorded_frames(self) -> int:
        with self._lock:
            return sum(b.shape[0] for b in self._blocks)

    @property
    def duration(self) -> float:
        return self.recorded_frames / float(self.samplerate)

    def start(self) -> None:
        if self.is_recording:
            raise RuntimeError("录音已在进行中")
        with self._lock:
            self._blocks = []
        self.meter.reset()
        self.overflow = 0
        try:
            self._stream = sd.InputStream(
                samplerate=self.samplerate,
                device=self.device,
                channels=self.channels,
                dtype="float32",
                blocksize=self.blocksize,
                latency=self.latency,
                callback=self._callback,
                extra_settings=make_extra_settings(self.exclusive),
            )
            self._stream.start()
        except Exception as exc:  # noqa: BLE001
            self._stream = None
            raise RuntimeError("无法打开输入设备 %d：%s" % (self.device, exc)) from exc

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        if status and getattr(status, "input_overflow", False):
            self.overflow += 1
        block = indata.copy()
        self.meter.push(block)
        with self._lock:
            self._blocks.append(block)

    def stop(self) -> np.ndarray:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
            except Exception:  # noqa: BLE001
                pass
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            blocks = self._blocks
            self._blocks = []
        if not blocks:
            return np.zeros((0, self.channels), dtype=np.float32)
        return np.concatenate(blocks, axis=0)
