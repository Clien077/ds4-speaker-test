# -*- coding: utf-8 -*-
"""音频 I/O、内置测试信号、重采样与"扬声器 → 麦克风"回环分析。

本模块只依赖 numpy；ffmpeg 为可选（用于解码 mp3/flac/ogg 等非 WAV 文件），
优先使用 imageio-ffmpeg 自带的 ffmpeg 可执行文件。

坐标系约定：所有采样数据均为 float32，取值范围标称 [-1, 1]，形状 (帧数, 声道数)。
"""

from __future__ import annotations

import math
import os
import shutil
import struct
import subprocess
import sys
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

#: 常见音频扩展名（非 WAV 需要 ffmpeg）
AUDIO_EXTS = (
    ".wav", ".wave", ".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a",
    ".aac", ".wma", ".aif", ".aiff", ".ape", ".wv", ".mp4", ".webm",
)

_EPS = 1e-12


class AudioError(RuntimeError):
    """音频解码 / 加载失败。"""


# --------------------------------------------------------------------------- #
# WAV 读写
# --------------------------------------------------------------------------- #

def _decode_pcm(raw: bytes, fmt: int, bits: int) -> np.ndarray:
    """把交错排列的裸 PCM 数据转成 float32（单声道一维数组）。"""
    if bits <= 0:
        raise AudioError("WAV 位深无效")
    frame = max(bits // 8, 1)
    usable = (len(raw) // frame) * frame
    raw = raw[:usable]

    if fmt == 3:  # IEEE float
        if bits == 32:
            return np.frombuffer(raw, "<f4").astype(np.float32)
        if bits == 64:
            return np.frombuffer(raw, "<f8").astype(np.float32)
        raise AudioError("不支持的浮点 WAV 位深: %d" % bits)

    if fmt == 1:  # 整数 PCM
        if bits == 8:  # 8bit WAV 是无符号
            return (np.frombuffer(raw, "<u1").astype(np.float32) - 128.0) / 128.0
        if bits == 16:
            return np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
        if bits == 24:
            b = np.frombuffer(raw, "<u1")
            n = (b.size // 3) * 3
            b = b[:n].reshape(-1, 3).astype(np.int32)
            v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
            v = np.where(v & 0x800000, v - 0x1000000, v)
            return v.astype(np.float32) / 8388608.0
        if bits == 32:
            return np.frombuffer(raw, "<i4").astype(np.float32) / 2147483648.0
        if bits == 64:
            return np.frombuffer(raw, "<i8").astype(np.float64).astype(np.float32) \
                / 9223372036854775808.0
        raise AudioError("不支持的整数 WAV 位深: %d" % bits)

    raise AudioError("不支持的 WAV 编码格式 (format tag = %d)" % fmt)


def parse_wav(data: bytes) -> Tuple[np.ndarray, int]:
    """解析内存中的 WAV 数据，返回 (samples[(n, ch)] float32, samplerate)。

    兼容 PCM / IEEE-float / WAVE_FORMAT_EXTENSIBLE，并处理 ffmpeg 输出到管道时
    长度字段为 0 或 0xFFFFFFFF 的流式头。
    """
    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise AudioError("不是 RIFF/WAVE 数据")

    pos = 12
    fmt: Optional[int] = None
    bits = 0
    ch = 0
    sr = 0
    payloads: List[bytes] = []

    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        body = pos + 8
        avail = len(data) - body
        if size == 0 or size == 0xFFFFFFFF or size > avail:
            size = avail  # 流式 / 截断的文件：取到结尾
        if cid == b"fmt ":
            if size < 16:
                raise AudioError("WAV 的 fmt 块过短")
            fmt, ch, sr = struct.unpack_from("<HHI", data, body)
            bits = struct.unpack_from("<H", data, body + 14)[0]
            if fmt == 0xFFFE:  # EXTENSIBLE：真实格式在 SubFormat GUID 的前两字节
                if size < 40:
                    raise AudioError("WAVE_FORMAT_EXTENSIBLE 的 fmt 块过短")
                fmt = struct.unpack_from("<H", data, body + 24)[0]
        elif cid == b"data":
            payloads.append(data[body:body + size])
        pos = body + size + (size & 1)

    if fmt is None or sr <= 0 or not payloads:
        raise AudioError("WAV 缺少 fmt / data 块")

    flat = _decode_pcm(b"".join(payloads), int(fmt), int(bits))
    if ch > 1:
        n = (flat.size // ch) * ch
        samples = flat[:n].reshape(-1, ch)
    else:
        samples = flat.reshape(-1, 1)
    return samples.astype(np.float32, copy=False), int(sr)


def read_wav(path: str) -> Tuple[np.ndarray, int]:
    with open(path, "rb") as fh:
        return parse_wav(fh.read())


def write_wav(path: str, samples: np.ndarray, samplerate: int, subtype: str = "int16") -> str:
    """写 WAV 文件。subtype: 'int16' 或 'float32'。"""
    x = np.asarray(samples, dtype=np.float32)
    if x.ndim == 1:
        x = x[:, None]
    n, ch = x.shape
    if subtype == "float32":
        payload = np.ascontiguousarray(x, dtype="<f4").tobytes()
        fmt_tag, bits = 3, 32
    else:
        clipped = np.clip(x, -1.0, 1.0)
        payload = (clipped * 32767.0).astype("<i2").tobytes()
        fmt_tag, bits = 1, 16
    block_align = max(ch, 1) * bits // 8
    byte_rate = int(samplerate) * block_align
    header = b"RIFF" + struct.pack("<I", 36 + len(payload)) + b"WAVE"
    header += b"fmt " + struct.pack(
        "<IHHIIHH", 16, fmt_tag, max(ch, 1), int(samplerate), byte_rate, block_align, bits
    )
    header += b"data" + struct.pack("<I", len(payload))
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(header)
        fh.write(payload)
    return path


# --------------------------------------------------------------------------- #
# ffmpeg 解码（任意格式 → float32）
# --------------------------------------------------------------------------- #

_FFMPEG_CACHE: List[Optional[str]] = []


def _ffmpeg_candidates() -> List[str]:
    """按优先级列出可能的 ffmpeg 路径（含 PyInstaller 打包后的解包目录）。"""
    out: List[str] = []
    bases: List[str] = []
    if getattr(sys, "frozen", False):
        bases.append(getattr(sys, "_MEIPASS", "") or "")
        bases.append(os.path.dirname(os.path.abspath(sys.executable)))
    for base in bases:
        if not base:
            continue
        out.append(os.path.join(base, "ffmpeg.exe"))
        out.append(os.path.join(base, "ffmpeg"))
        bin_dir = os.path.join(base, "imageio_ffmpeg", "binaries")
        if os.path.isdir(bin_dir):
            for name in sorted(os.listdir(bin_dir)):
                if name.lower().startswith("ffmpeg"):
                    out.append(os.path.join(bin_dir, name))
    try:
        import imageio_ffmpeg  # type: ignore

        out.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:  # noqa: BLE001
        pass
    which = shutil.which("ffmpeg")
    if which:
        out.append(which)
    return out


def find_ffmpeg() -> Optional[str]:
    """定位可用的 ffmpeg：打包目录 > imageio-ffmpeg 自带 > PATH。"""
    if _FFMPEG_CACHE:
        return _FFMPEG_CACHE[0]
    exe: Optional[str] = None
    for candidate in _ffmpeg_candidates():
        if candidate and os.path.isfile(candidate):
            exe = candidate
            break
    _FFMPEG_CACHE.append(exe)
    return exe


def _no_window_flags() -> int:
    if sys.platform == "win32":
        return 0x08000000  # CREATE_NO_WINDOW
    return 0


def ffmpeg_decode(path: str, samplerate: int, channels: int) -> Tuple[np.ndarray, int]:
    """用 ffmpeg 解码成 WAV 字节流，再用本地解析器读出。

    重采样与声道转换都交给 ffmpeg 的 swresample 完成（质量优于自实现）。
    """
    exe = find_ffmpeg()
    if not exe:
        raise AudioError("未找到 ffmpeg，无法解码该格式（请安装 ffmpeg 或改用 WAV 文件）")
    cmd = [
        exe, "-hide_banner", "-v", "error", "-nostdin", "-y",
        "-i", path,
        "-map", "0:a:0",
        "-f", "wav", "-acodec", "pcm_f32le",
        "-ar", str(int(samplerate)), "-ac", str(int(channels)),
        "-",
    ]
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=_no_window_flags(), check=False,
        )
    except OSError as exc:
        raise AudioError("调用 ffmpeg 失败: %s" % exc) from exc
    if proc.returncode != 0 or not proc.stdout:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        tail = " | ".join(err[-3:]) if err else "未知错误"
        raise AudioError("ffmpeg 解码失败 (%d): %s" % (proc.returncode, tail))
    samples, sr = parse_wav(proc.stdout)
    return samples, sr


# --------------------------------------------------------------------------- #
# 重采样 / 声道映射
# --------------------------------------------------------------------------- #

def resample(x: np.ndarray, sr_in: float, sr_out: float, taps: int = 32) -> np.ndarray:
    """分块 Blackman 窗 sinc 重采样（含抗混叠），无需 scipy。"""
    y = np.asarray(x, dtype=np.float32)
    if y.ndim == 1:
        y = y[:, None]
    if y.shape[0] == 0 or abs(float(sr_in) - float(sr_out)) < 1e-6:
        return y

    n_in, n_ch = y.shape
    n_out = int(round(n_in * float(sr_out) / float(sr_in)))
    if n_out <= 0:
        return np.zeros((0, n_ch), dtype=np.float32)

    ratio = float(sr_in) / float(sr_out)
    cutoff = min(1.0, float(sr_out) / float(sr_in))  # 降采样时压缩带宽
    taps = int(max(8, taps))
    offs = np.arange(-taps + 1, taps + 1, dtype=np.float64)
    out = np.empty((n_out, n_ch), dtype=np.float32)

    block = 32768
    for start in range(0, n_out, block):
        stop = min(start + block, n_out)
        pos = np.arange(start, stop, dtype=np.float64) * ratio
        base = np.floor(pos)
        d = pos[:, None] - (base[:, None] + offs[None, :])
        w = np.sinc(d * cutoff) * cutoff
        u = d / taps
        win = np.where(
            np.abs(u) < 1.0,
            0.42 + 0.5 * np.cos(np.pi * u) + 0.08 * np.cos(2.0 * np.pi * u),
            0.0,
        )
        w *= win
        # 用"带符号"的权重和做直流增益归一化（用绝对值会引入固定衰减）
        gain = w.sum(axis=1, keepdims=True)
        w = w / np.where(np.abs(gain) < 1e-9, 1.0, gain)
        idx = np.clip((base[:, None] + offs[None, :]).astype(np.int64), 0, n_in - 1)
        out[start:stop] = np.einsum("ij,ijk->ik", w, y[idx], optimize=True)
    return out


def map_channels(x: np.ndarray, channels: int) -> np.ndarray:
    """把音频适配到目标声道数（单声道复制 / 多声道取前 N 或求平均）。"""
    y = np.asarray(x, dtype=np.float32)
    if y.ndim == 1:
        y = y[:, None]
    cur = y.shape[1]
    channels = int(channels)
    if cur == channels:
        return y
    if channels <= 0:
        return y
    if cur == 1:
        return np.repeat(y, channels, axis=1)
    if channels == 1:
        return y.mean(axis=1, keepdims=True)
    if cur < channels:
        pad = np.zeros((y.shape[0], channels - cur), dtype=np.float32)
        return np.concatenate([y, pad], axis=1)
    return y[:, :channels]


def route_channels(x: np.ndarray, mode: str) -> np.ndarray:
    """声道路由：both / left / right（用于分别测试左右耳机、或手柄内置喇叭）。"""
    y = np.asarray(x, dtype=np.float32)
    if y.ndim == 1:
        y = y[:, None]
    mode = (mode or "both").lower()
    if y.shape[1] < 2 or mode == "both":
        return y
    out = np.zeros_like(y)
    if mode == "left":
        out[:, 0] = y[:, 0]
    elif mode == "right":
        out[:, 1] = y[:, 1]
    elif mode == "swap":
        out[:, 0] = y[:, 1]
        out[:, 1] = y[:, 0]
    else:
        return y
    return out


# --------------------------------------------------------------------------- #
# 音频加载
# --------------------------------------------------------------------------- #

def load_audio(
    path: str,
    samplerate: Optional[int] = None,
    channels: Optional[int] = None,
    prefer_ffmpeg: bool = False,
) -> Tuple[np.ndarray, int]:
    """加载任意音频文件并适配到目标采样率 / 声道数。

    WAV 走内置解析器（零依赖、零延迟）；其它格式或需要重采样/变声道时交给 ffmpeg。
    """
    if not os.path.isfile(path):
        raise AudioError("文件不存在: %s" % path)

    samples: Optional[np.ndarray] = None
    sr = 0
    if not prefer_ffmpeg:
        try:
            with open(path, "rb") as fh:
                head = fh.read(16)
            if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
                samples, sr = read_wav(path)
        except (AudioError, OSError):
            samples, sr = None, 0

    rate_mismatch = bool(samplerate) and int(samplerate) != int(sr)
    chan_mismatch = bool(channels) and samples is not None and samples.shape[1] != int(channels)
    need_ffmpeg = samples is None or rate_mismatch or chan_mismatch

    if need_ffmpeg and find_ffmpeg():
        target_sr = int(samplerate or sr or 48000)
        target_ch = int(channels or (samples.shape[1] if samples is not None else 2))
        samples, sr = ffmpeg_decode(path, target_sr, target_ch)
    elif samples is None:
        raise AudioError("无法解码该音频文件（未找到 ffmpeg）: %s" % path)

    assert samples is not None
    if samplerate and int(samplerate) != int(sr):
        samples = resample(samples, sr, int(samplerate))
        sr = int(samplerate)
    if channels:
        samples = map_channels(samples, int(channels))
    return np.ascontiguousarray(samples, dtype=np.float32), int(sr)


def slice_audio(
    samples: np.ndarray, samplerate: int, start_s: float = 0.0, duration_s: float = 0.0
) -> np.ndarray:
    """按秒裁剪音频（duration<=0 表示到结尾）。"""
    a = int(max(0.0, start_s) * samplerate)
    if duration_s and duration_s > 0:
        b = a + int(duration_s * samplerate)
        return samples[a:b]
    return samples[a:]


def audio_info(path: str) -> Dict[str, object]:
    """只读探测（不重采样），用于界面显示。"""
    info: Dict[str, object] = {"path": path, "ok": False, "error": ""}
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
        if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
            samples, sr = read_wav(path)
        else:
            samples, sr = ffmpeg_decode(path, 48000, 2)
            samples = samples  # 已重采样到 48k，时长信息仍准确
        info.update(
            ok=True,
            samplerate=int(sr),
            channels=int(samples.shape[1]),
            duration=float(samples.shape[0]) / float(sr or 1),
            frames=int(samples.shape[0]),
        )
    except Exception as exc:  # noqa: BLE001 - 界面需要把任何失败都显示出来
        info["error"] = str(exc)
    return info


# --------------------------------------------------------------------------- #
# 内置测试信号
# --------------------------------------------------------------------------- #

def _fade(x: np.ndarray, samplerate: int, ms: float = 10.0) -> np.ndarray:
    """首尾淡入淡出，避免爆音（爆音会被误判成扬声器故障）。"""
    n = int(samplerate * ms / 1000.0)
    n = min(n, x.shape[0] // 2)
    if n <= 1:
        return x
    ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
    x[:n] *= ramp[:, None]
    x[-n:] *= ramp[::-1, None]
    return x


def _gap(samplerate: int, channels: int, seconds: float) -> np.ndarray:
    return np.zeros((max(1, int(seconds * samplerate)), channels), dtype=np.float32)


def tone(
    freq: float = 1000.0, duration: float = 3.0, samplerate: int = 48000,
    channels: int = 2, amplitude: float = 0.25, fade_ms: float = 10.0,
) -> np.ndarray:
    n = max(1, int(round(duration * samplerate)))
    t = np.arange(n, dtype=np.float64) / float(samplerate)
    mono = (amplitude * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)
    x = np.repeat(mono[:, None], channels, axis=1)
    return _fade(x, samplerate, fade_ms)


def sweep(
    f0: float = 20.0, f1: float = 20000.0, duration: float = 8.0, samplerate: int = 48000,
    channels: int = 2, amplitude: float = 0.25, logarithmic: bool = True,
) -> np.ndarray:
    """对数（或线性）扫频：最常用的扬声器全频段测试信号。"""
    n = max(2, int(round(duration * samplerate)))
    t = np.arange(n, dtype=np.float64) / float(samplerate)
    u = t / float(duration)
    if logarithmic and f0 > 0 and f1 > f0:
        k = math.log(f1 / f0)
        phase = 2.0 * np.pi * f0 * duration / k * (np.exp(u * k) - 1.0)
    else:
        phase = 2.0 * np.pi * (f0 * t + 0.5 * (f1 - f0) * t * u)
    mono = (amplitude * np.sin(phase)).astype(np.float32)
    x = np.repeat(mono[:, None], channels, axis=1)
    return _fade(x, samplerate, 15.0)


def noise(
    duration: float = 5.0, samplerate: int = 48000, channels: int = 2,
    amplitude: float = 0.2, kind: str = "white", seed: Optional[int] = None,
) -> np.ndarray:
    """白噪声 / 粉噪声（各声道独立，便于检查声道分离）。"""
    rng = np.random.default_rng(seed)
    n = max(1, int(round(duration * samplerate)))
    cols = []
    for _ in range(channels):
        w = rng.standard_normal(n)
        if kind == "pink":
            spec = np.fft.rfft(w)
            freqs = np.fft.rfftfreq(n, 1.0 / samplerate)
            freqs[0] = freqs[1] if freqs.size > 1 else 1.0
            spec = spec / np.sqrt(freqs)
            w = np.fft.irfft(spec, n)
        peak = float(np.max(np.abs(w))) or 1.0
        cols.append((w / peak * amplitude).astype(np.float32))
    x = np.stack(cols, axis=1) if channels > 1 else cols[0][:, None]
    return _fade(x, samplerate, 10.0)


def channel_test(
    samplerate: int = 48000, channels: int = 2, amplitude: float = 0.25, freq: float = 1000.0
) -> np.ndarray:
    """逐声道点名：左 → 右 → 全部（检查耳机左右与声道接线）。"""
    parts: List[np.ndarray] = []
    for idx in range(max(1, channels)):
        mono = tone(freq, 1.4, samplerate, 1, amplitude)
        block = np.zeros((mono.shape[0], channels), dtype=np.float32)
        block[:, idx] = mono[:, 0]
        parts.append(block)
        parts.append(_gap(samplerate, channels, 0.35))
    both = tone(freq, 1.4, samplerate, channels, amplitude)
    parts.append(both)
    return np.concatenate(parts, axis=0)


def polarity_test(
    samplerate: int = 48000, channels: int = 2, amplitude: float = 0.25, freq: float = 120.0
) -> np.ndarray:
    """同相 → 反相：配合麦克风可检查相位（反相时低频明显抵消）。"""
    same = tone(freq, 1.5, samplerate, channels, amplitude)
    inv = same.copy()
    if channels > 1:
        inv[:, 1] *= -1.0
    return np.concatenate([same, _gap(samplerate, channels, 0.4), inv], axis=0)


_DTMF_ROWS = {"1": 697, "2": 697, "3": 697, "4": 770, "5": 770,
              "6": 770, "7": 852, "8": 852, "9": 852, "0": 941}
_DTMF_COLS = {"1": 1209, "2": 1336, "3": 1477, "4": 1209, "5": 1336,
              "6": 1477, "7": 1209, "8": 1336, "9": 1477, "0": 1336}


def dtmf_sequence(
    samplerate: int = 48000, channels: int = 2, amplitude: float = 0.2,
    digits: str = "1234567890", tone_s: float = 0.35, gap_s: float = 0.18,
) -> np.ndarray:
    """双音多频序列：用固定频率组合快速判断频响是否明显失真。"""
    parts: List[np.ndarray] = []
    for d in digits:
        if d not in _DTMF_ROWS:
            continue
        n = max(1, int(tone_s * samplerate))
        t = np.arange(n, dtype=np.float64) / float(samplerate)
        sig = np.sin(2 * np.pi * _DTMF_ROWS[d] * t) + np.sin(2 * np.pi * _DTMF_COLS[d] * t)
        sig = (sig / 2.0 * amplitude).astype(np.float32)
        block = np.repeat(sig[:, None], channels, axis=1)
        parts.append(_fade(block, samplerate, 6.0))
        parts.append(_gap(samplerate, channels, gap_s))
    return np.concatenate(parts, axis=0) if parts else _gap(samplerate, channels, 0.1)


def silence(duration: float = 1.0, samplerate: int = 48000, channels: int = 2) -> np.ndarray:
    return _gap(samplerate, channels, duration)


#: 内置信号：名称 -> (中文说明, 默认时长秒)
SIGNAL_INFO: Dict[str, Tuple[str, float]] = {
    "tone": ("单音（默认 1 kHz，可调频率）", 3.0),
    "sweep": ("对数扫频 20 Hz → 20 kHz（听杂音/破音）", 8.0),
    "sweep_speech": ("语音频段扫频 200 Hz → 6 kHz（音量与清晰度）", 5.0),
    "white": ("白噪声（全频段压力测试）", 5.0),
    "pink": ("粉噪声（贴近音乐能量分布）", 5.0),
    "channels": ("逐声道点名：左 → 右 → 全部", 5.0),
    "polarity": ("同相 / 反相测试（低频相位）", 4.0),
    "dtmf": ("双音多频 1-0（频响快速校验）", 6.0),
    "silence": ("静音（链路预检 / 底噪）", 1.0),
    "all": ("完整测试序列（自动依次播放全部项目）", 0.0),
}

#: "all" 序列里的每一项
_ALL_STEPS = (
    ("tone", "单音 1 kHz"),
    ("sweep", "全频段对数扫频"),
    ("channels", "左右声道点名"),
    ("pink", "粉噪声"),
    ("polarity", "同相/反相"),
    ("dtmf", "双音多频"),
)


def build_all_tests(
    samplerate: int = 48000, channels: int = 2, amplitude: float = 0.25,
    freq: float = 1000.0, f0: float = 20.0, f1: float = 20000.0,
) -> Tuple[np.ndarray, List[Tuple[float, str]]]:
    """拼接完整测试序列，返回 (音频, [(起始秒, 项目名), ...])。"""
    parts: List[np.ndarray] = []
    marks: List[Tuple[float, str]] = []
    cursor = 0.0
    for key, label in _ALL_STEPS:
        sig, _ = build_signal(
            key, samplerate=samplerate, channels=channels, amplitude=amplitude,
            freq=freq, f0=f0, f1=f1, duration=None,
        )
        marks.append((cursor, label))
        parts.append(sig)
        cursor += sig.shape[0] / float(samplerate)
        gap = _gap(samplerate, channels, 0.4)
        parts.append(gap)
        cursor += 0.4
    return np.concatenate(parts, axis=0), marks


def build_signal(
    name: str, samplerate: int = 48000, channels: int = 2, duration: Optional[float] = None,
    amplitude: float = 0.25, freq: float = 1000.0, f0: float = 20.0, f1: float = 20000.0,
    seed: Optional[int] = None,
) -> Tuple[np.ndarray, str]:
    """按名称生成内置测试信号，返回 (音频, 说明文字)。"""
    key = (name or "tone").strip().lower()
    default_dur = SIGNAL_INFO.get(key, ("", 3.0))[1]
    dur = float(duration) if duration else default_dur
    if key == "all":
        sig, _ = build_all_tests(samplerate, channels, amplitude, freq, f0, f1)
        return sig, "完整测试序列（约 %.0f 秒）" % (sig.shape[0] / samplerate)
    if key == "tone":
        return tone(freq, dur or 3.0, samplerate, channels, amplitude), "单音 %.0f Hz" % freq
    if key == "sweep":
        return sweep(f0, f1, dur or 8.0, samplerate, channels, amplitude), \
            "对数扫频 %.0f Hz → %.0f Hz" % (f0, f1)
    if key == "sweep_speech":
        return sweep(200.0, 6000.0, dur or 5.0, samplerate, channels, amplitude), \
            "语音频段扫频 200 Hz → 6000 Hz"
    if key == "white":
        return noise(dur or 5.0, samplerate, channels, amplitude, "white", seed), "白噪声"
    if key == "pink":
        return noise(dur or 5.0, samplerate, channels, amplitude, "pink", seed), "粉噪声"
    if key == "channels":
        return channel_test(samplerate, channels, amplitude, freq), "左右声道点名"
    if key == "polarity":
        return polarity_test(samplerate, channels, amplitude), "同相 / 反相测试"
    if key == "dtmf":
        return dtmf_sequence(samplerate, channels, amplitude), "双音多频序列"
    if key == "silence":
        return silence(dur or 1.0, samplerate, channels), "静音"
    raise AudioError("未知的内置信号: %s（可选：%s）" % (name, ", ".join(SIGNAL_INFO)))


# --------------------------------------------------------------------------- #
# 电平换算
# --------------------------------------------------------------------------- #

def to_db(value: float) -> float:
    return 20.0 * math.log10(max(float(value), 1e-9))


def signal_stats(x: np.ndarray) -> Dict[str, object]:
    y = np.asarray(x, dtype=np.float32)
    if y.ndim == 1:
        y = y[:, None]
    if y.size == 0:
        return {"frames": 0, "channels": 0, "peak_db": -180.0, "rms_db": -180.0,
                "clipped": 0, "peak": 0.0, "rms": 0.0}
    peak = float(np.max(np.abs(y)))
    rms = float(np.sqrt(np.mean(y.astype(np.float64) ** 2)))
    return {
        "frames": int(y.shape[0]),
        "channels": int(y.shape[1]),
        "peak": peak,
        "rms": rms,
        "peak_db": to_db(peak),
        "rms_db": to_db(rms),
        "clipped": int(np.count_nonzero(np.abs(y) >= 0.999)),
    }


# --------------------------------------------------------------------------- #
# 回环分析：播放到 DS4 扬声器 → 用 DS4 麦克风拾音 → 判断是否正常
# --------------------------------------------------------------------------- #

def _to_mono(x: np.ndarray) -> np.ndarray:
    y = np.asarray(x, dtype=np.float32)
    if y.ndim == 1:
        return y
    if y.shape[1] == 1:
        return y[:, 0]
    return y.mean(axis=1)


def _mean_mag_spectrum(
    x: np.ndarray, samplerate: int, nfft: int = 4096, band: Tuple[float, float] = (80.0, 8000.0)
) -> Tuple[np.ndarray, np.ndarray]:
    """平均幅度谱（线性），带限到 band 内。

    默认上限取 8 kHz：一来小喇叭与麦克风阵列在这个频段之外本就没什么能量，
    二来两个流采样率不同时参考信号需要重采样，靠近奈奎斯特的镜像会污染比较结果。
    """
    mono = _to_mono(x)
    if mono.size < 512:
        mono = np.pad(mono, (0, 512 - mono.size))
    nfft = int(min(nfft, 1 << int(np.floor(np.log2(max(mono.size, 512))))))
    nfft = max(nfft, 512)
    hop = nfft // 2
    win = np.hanning(nfft).astype(np.float32)
    frames = 1 + max(0, (mono.size - nfft) // hop)
    acc = np.zeros(nfft // 2 + 1, dtype=np.float64)
    for i in range(frames):
        seg = mono[i * hop:i * hop + nfft] * win
        acc += np.abs(np.fft.rfft(seg))
    acc /= float(max(frames, 1))
    freqs = np.fft.rfftfreq(nfft, 1.0 / samplerate)
    mask = (freqs >= band[0]) & (freqs <= band[1])
    if not np.any(mask):
        mask = np.ones_like(freqs, dtype=bool)
    return freqs[mask], acc[mask]


def _envelope(x: np.ndarray, samplerate: int, frame_ms: float = 10.0) -> Optional[np.ndarray]:
    mono = _to_mono(x)
    n = max(1, int(samplerate * frame_ms / 1000.0))
    k = mono.size // n
    if k < 8:
        return None
    return np.sqrt(np.mean(mono[:k * n].reshape(k, n).astype(np.float64) ** 2, axis=1))


def _best_envelope_lag(
    ref_env: np.ndarray, rec_env: np.ndarray, max_lag: int
) -> Tuple[int, float]:
    """在 0..max_lag 帧范围内找最佳归一化相关及其滞后。"""
    a0 = ref_env - ref_env.mean()
    na = float(np.linalg.norm(a0))
    if na < _EPS:
        return 0, 0.0
    best_lag, best_corr = 0, -2.0
    for lag in range(0, min(max_lag, rec_env.size - 1) + 1):
        seg = rec_env[lag:]
        m = min(a0.size, seg.size)
        if m < 8:
            break
        aa = a0[:m]
        bb = seg[:m] - seg[:m].mean()
        nb = float(np.linalg.norm(bb))
        if nb < _EPS:
            continue
        corr = float(np.dot(aa, bb) / (na * nb))
        if corr > best_corr:
            best_lag, best_corr = lag, corr
    return best_lag, max(best_corr, -1.0)


def analyze_loopback(
    reference: np.ndarray, recorded: np.ndarray, samplerate: int,
    preroll_s: float = 0.0, silence_db: float = -50.0,
) -> Dict[str, object]:
    """比较"送出的参考音频"与"手柄麦克风录回来的音频"。

    返回的判定是启发式的，但足以区分：完全没声音 / 有声音但很弱 / 正常出声 / 破音。
    """
    stats = signal_stats(recorded)
    # 只取"应该在出声"的那一段时间做电平与频谱判定，避免前后静音段把结论带偏
    ref_seconds = reference.shape[0] / float(samplerate)
    a = int(max(0.0, preroll_s) * samplerate)
    b = min(recorded.shape[0], int((max(0.0, preroll_s) + ref_seconds + 0.2) * samplerate))
    main = recorded[a:b] if (b - a) > 0.05 * samplerate else recorded
    stats_main = signal_stats(main)

    # 用推流前后的录音估计这一段自己的底噪：频谱相似度对"只有噪声"的录音没有鉴别力，
    # 所以判定必须看"播放窗口比底噪高出多少 dB"。
    # 注意手柄有音频缓冲，推流结束后尾段可能还在响，所以取前后两段里**更安静**的那个当底噪。
    floors: List[float] = []
    if a > 0.1 * samplerate:
        seg = recorded[:a].astype(np.float64)
        floors.append(to_db(float(np.sqrt(np.mean(seg ** 2)))))
    if recorded.shape[0] - b > 0.1 * samplerate:
        seg = recorded[b:].astype(np.float64)
        floors.append(to_db(float(np.sqrt(np.mean(seg ** 2)))))
    floor_db: Optional[float] = min(floors) if floors else None
    snr_db: Optional[float] = None if floor_db is None else float(stats_main["rms_db"]) - floor_db

    result: Dict[str, object] = {
        "rec_peak_db": stats["peak_db"],
        "rec_rms_db": stats["rms_db"],
        "rec_peak_db_main": stats_main["peak_db"],
        "rec_rms_db_main": stats_main["rms_db"],
        "floor_db": floor_db,
        "snr_db": snr_db,
        "clipped_ratio": (stats["clipped"] / stats["frames"]) if stats["frames"] else 0.0,
        "spec_sim": 0.0,
        "env_corr": None,
        "delay_s": None,
        "ref_dom_hz": None,
        "rec_dom_hz": None,
        "verdict": "unclear",
        "verdict_text": "无法判定",
        "detail": "",
    }

    if stats["frames"] == 0:
        result.update(verdict="silent", verdict_text="没有录到任何数据",
                      detail="录音设备未返回数据")
        return result

    # 1) 频谱形状相似度：即使参考信号是恒定单音也能判定
    try:
        f_ref, m_ref = _mean_mag_spectrum(reference, samplerate)
        f_rec, m_rec = _mean_mag_spectrum(main, samplerate)
    except Exception as exc:  # noqa: BLE001
        result["detail"] = "频谱分析失败: %s" % exc
        return result

    n = min(m_ref.size, m_rec.size)
    a, b = m_ref[:n], m_rec[:n]
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    spec_sim = float(np.dot(a, b) / (na * nb)) if na > _EPS and nb > _EPS else 0.0
    result["spec_sim"] = spec_sim
    if f_ref.size and m_ref.size:
        result["ref_dom_hz"] = float(f_ref[int(np.argmax(m_ref))])
    if f_rec.size and m_rec.size:
        result["rec_dom_hz"] = float(f_rec[int(np.argmax(m_rec))])

    # 2) 包络相关：有起伏的信号（音乐/语音/DTMF/左右声道点名）能给出相似度与延迟
    ref_env = _envelope(reference, samplerate)
    rec_env = _envelope(recorded, samplerate)
    env_corr: Optional[float] = None
    if ref_env is not None and rec_env is not None and ref_env.size > 8:
        if float(ref_env.std()) > 0.02 * max(float(ref_env.mean()), _EPS):
            max_lag = int(min(rec_env.size - 1, 200))  # 最多找 2 秒延迟（10 ms/帧）
            lag, corr = _best_envelope_lag(ref_env, rec_env, max_lag)
            env_corr = corr
            result["env_corr"] = corr
            if corr >= 0.30:
                # 只有相关足够强时才敢报延迟；噪声类信号会给出无意义的随机滞后
                result["delay_s"] = max(0.0, lag * 0.01 - float(preroll_s))
                result["lag_s_raw"] = lag * 0.01

    # 3) 判定
    peak_db = float(stats_main["peak_db"])
    clipped_ratio = float(result["clipped_ratio"])
    env_usable = env_corr is not None and env_corr >= 0.30
    score = max(spec_sim, env_corr if env_usable else -1.0)
    snr_text = "" if snr_db is None else "（比底噪高 %.1f dB）" % snr_db

    if peak_db < silence_db:
        verdict, text = "silent", "麦克风几乎录不到声音（扬声器没出声 / 音量太低 / 端点或麦克风选错）%s" % snr_text
    elif snr_db is not None and snr_db < 6.0:
        verdict, text = "silent", "播放窗口内没有比底噪更响的声音，扬声器基本没出声%s" % snr_text
    elif clipped_ratio > 5e-3:
        verdict, text = "clipped", "录到严重削波，音量过大或扬声器已失真"
    elif score >= 0.60 or (snr_db is not None and snr_db >= 20.0 and score >= 0.40):
        verdict, text = "pass", "扬声器正常出声，且与测试音频高度一致%s" % snr_text
    elif score >= 0.35 or (snr_db is not None and snr_db >= 12.0):
        verdict, text = "weak", "能听到声音，但与测试音频匹配度偏低（音量偏小/失真/底噪大）%s" % snr_text
    else:
        verdict, text = "unclear", "录到了声音，但不像是在播放测试音频（可能只录到环境噪声）%s" % snr_text

    result.update(verdict=verdict, verdict_text=text, score=score)
    detail = "频谱相似度 %.2f" % spec_sim
    if env_usable:
        detail += "，包络相关 %.2f" % env_corr
    elif env_corr is not None:
        detail += "，包络相关 %.2f（该信号无包络起伏，延迟不可测）" % env_corr
    if snr_db is not None:
        detail += "，信噪比 %.1f dB" % snr_db
    result["detail"] = detail
    return result
