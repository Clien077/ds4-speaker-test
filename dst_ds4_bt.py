# -*- coding: utf-8 -*-
"""PS4 DualShock 4 内置扬声器的**蓝牙 HID + SBC 音频推流**实现。

Windows 上并没有驱动 DS4 内置喇叭的音频端点：那个 ``无线控制器`` 音频设备其实是
USB 音频类（UAC，走 3.5mm 耳机口）。PS4 驱动内置喇叭用的是私有通路 ——
把 **SBC 编码的音频帧**塞进 HID 输出报告（0x14 / 0x17）通过蓝牙发给手柄。

本模块只做这条通路：
    自定义音频 --ffmpeg(SBC 编码)--> 帧列表 --打包--> HID 输出报告 --蓝牙--> 手柄喇叭

参考实现：nefarius/DS4AudioStreamer（MIT）与 BlueZ/libsbc。协议要点：

* 每条音频报告头部 6 字节：``[报告ID][0x40][0xA2][帧计数低][帧计数高][输出通路]``
* ``0x17`` 报告长 462 字节、装 4 个 SBC 帧；``0x14`` 报告长 270 字节、装 2 个帧
* 第 6 字节（下标 5）：``0x02`` = 内置喇叭，``0x24`` = 耳机口
* 报告末尾 4 字节是 BT CRC32（初值 ``~0xEADA2D49``，小端序）
* 开始推流前要先发一条 78 字节的 ``0x11`` 控制报告把音量打开（0x80 位 + 音量 0x50）
* SBC 参数需为 32 kHz / 8 子带 / 16 块 / 立体声，帧长约 108~113 字节
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import os
import re
import subprocess
import sys
import threading
import time
import zlib
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from dst_audio import AudioError, find_ffmpeg, to_db

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

SONY_VID = 0x054C

#: 支持的 PlayStation 手柄（PID -> 名称）
DS4_PIDS: Dict[int, str] = {
    0x05C4: "DualShock 4 v1 (CUH-ZCT1)",
    0x09CC: "DualShock 4 v2 (CUH-ZCT2)",
    0x0BA0: "DualShock 4 (USB 无线适配器)",
}
DUALSENSE_PIDS: Dict[int, str] = {
    0x0CE6: "DualSense (PS5)",
    0x0DF2: "DualSense Edge (PS5)",
}
ALL_PIDS: Dict[int, str] = {**DS4_PIDS, **DUALSENSE_PIDS}

#: 蓝牙 HID 服务的 UUID，出现在 Windows 的蓝牙 HID 设备路径里
BT_SERVICE_HINT = "00001124-0000-1000-8000-00805f9b34fb"

SBC_SAMPLE_RATE = 32000
#: 32 kHz / (16 块 × 8 子带) = 每帧 128 个采样 -> 250 帧/秒
SBC_FRAMES_PER_SECOND = 250.0
SBC_FRAME_SAMPLES = 128

CTRL_REPORT_ID = 0x11
CTRL_REPORT_SIZE = 78
AUDIO_REPORT_ID_4 = 0x17
AUDIO_REPORT_SIZE_4 = 462
AUDIO_REPORT_ID_2 = 0x14
AUDIO_REPORT_SIZE_2 = 270

#: pywinusb 枚举到的"集合级最大输出报告长度"（实测本机 DS4 为 547）。
#: 参考实现是**按这个长度写满**，把 CRC 放在各报告真实长度的末尾，
#: 写 462/270 字节的短报告会被驱动拒绝或让手柄丢掉整条报告。
DEFAULT_WRITE_SIZE = 547

#: 报告第 3 字节：音频报告(0x14/0x17/0x15/0x19)为 0xA0；
#: 控制报告 0x11 是 audio_control=0x20（见 build_control_report）。
AUDIO_BYTE2 = 0xA0

#: 音频报告第 6 字节：输出通路。**0x02 = 内置扬声器，0x24 = 3.5mm 耳机孔**
#: （来源：PS4 真机抓包 + Linux hid-playstation.c + Unity 实现三处互证）
OUTPUT_DEFAULT = 0x02
OUTPUT_ALTERNATIVES = (0x02, 0x24, 0x00, 0x03)

#: 零填充时的参考帧长（仅供参考/校验用；手柄按帧头解析，帧长不固定）
SBC_FRAME_LENGTH_PS4 = 112
SBC_SUBBANDS = 8
SBC_BLOCKS = 16
SBC_CHANNELS = 2

#: 默认跟随参考实现：不指定 -b:a / -sbc_delay，用 ffmpeg 默认的 SBC 参数
#: （32 kHz / 8 子带 / 16 块 / 联合立体声 / 比特池 26 -> 65 字节帧，实测可用）
SBC_DEFAULT_BITRATE = 0
SBC_DEFAULT_DELAY = ""

_BLOCKS_TABLE = {0: 4, 1: 8, 2: 12, 3: 16}
_FREQ_TABLE = {0: 16000, 1: 32000, 2: 44100, 3: 48000}
_MODE_NAMES = {0: "MONO", 1: "DUAL_CHANNEL", 2: "STEREO", 3: "JOINT_STEREO"}


def bitpool_for_frame_length(frame_length: int = SBC_FRAME_LENGTH_PS4) -> int:
    """反推比特池：ffmpeg 的帧长公式（立体声、8 子带、16 块）。"""
    # frame_length = 4 + (4*8*2)/8 + ceil(16*bp/8) = 12 + 2*bp
    return max(1, (int(frame_length) - 12) // 2)


def bitrate_for_bitpool(bitpool: int, samplerate: int = SBC_SAMPLE_RATE) -> int:
    """反推 ffmpeg 的 -b:a：bitpool = (bit_rate/250 - 88) / 16（立体声/8 子带/16 块）。"""
    d = SBC_BLOCKS  # dual=0 -> d = blocks
    numerator = (bitpool * d + 4 * SBC_SUBBANDS * SBC_CHANNELS + 32 - d // 2)
    return int(round(samplerate * numerator / float(SBC_SUBBANDS * SBC_BLOCKS)))


SBC_DEFAULT_BITPOOL = bitpool_for_frame_length(SBC_FRAME_LENGTH_PS4)
HIGH_QUALITY_BITRATE = bitrate_for_bitpool(SBC_DEFAULT_BITPOOL)
MAX_FRAMES_PER_REPORT = 4


# --------------------------------------------------------------------------- #
# BT CRC32
# --------------------------------------------------------------------------- #

def crc32_bt(payload: bytes, prefix: bytes = b"\xa2") -> int:
    """DS4 蓝牙输出报告末尾 4 字节 CRC32（小端），Linux hid-playstation.c 算法：

        crc = crc32_le(0xFFFFFFFF, 0xA2, 1);  crc = ~crc32_le(crc, payload)

    等价于"对 0xA2 + payload 做标准 CRC-32"，用 zlib 的续算接口即可。
    """
    crc = zlib.crc32(prefix, 0)
    return zlib.crc32(payload, crc) & 0xFFFFFFFF


def crc32_bt_reference(payload: bytes, prefix: bytes = b"\xa2") -> int:
    """逐位实现（仅用于自检时交叉验证 crc32_bt）。"""
    def crc32_le(init: int, data: bytes) -> int:
        crc = init & 0xFFFFFFFF
        for byte in data:
            crc ^= byte
            for _ in range(8):
                crc = ((crc >> 1) ^ 0xEDB88320) if (crc & 1) else (crc >> 1)
        return crc & 0xFFFFFFFF

    crc = crc32_le(0xFFFFFFFF, prefix)
    return (~crc32_le(crc, payload)) & 0xFFFFFFFF


# --------------------------------------------------------------------------- #
# HID 设备查找与写入
# --------------------------------------------------------------------------- #

@dataclass
class Ds4HidDevice:
    """一个手柄 HID 接口。"""

    path: str
    vendor_id: int
    product_id: int
    product_name: str
    manufacturer: str = ""
    output_report_length: int = 0
    usage_page: int = 0
    usage: int = 0

    @property
    def model(self) -> str:
        return ALL_PIDS.get(self.product_id, "PlayStation 手柄")

    @property
    def transport(self) -> str:
        p = (self.path or "").lower()
        if BT_SERVICE_HINT in p or "bthenum" in p or "bth" in p:
            return "BT"
        if re.search(r"vid_[0-9a-f]{4}&pid_[0-9a-f]{4}&mi_", p) or "usb" in p:
            return "USB"
        return "?"

    @property
    def is_bluetooth(self) -> bool:
        return self.transport == "BT"

    @property
    def label(self) -> str:
        return "%s · %s · 输出报告 %d 字节" % (self.model, self.transport, self.output_report_length)


def find_ds4_hid_devices(include_dualsense: bool = True) -> List[Ds4HidDevice]:
    """列出所有 Sony 手柄 HID 接口（蓝牙与 USB 都会列出，并标注连接方式）。"""
    try:
        import pywinusb.hid as pyhid  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("需要 pywinusb 才能访问手柄 HID 接口：%s" % exc) from exc

    wanted = dict(DS4_PIDS)
    if include_dualsense:
        wanted.update(DUALSENSE_PIDS)

    found: List[Ds4HidDevice] = []
    for dev in pyhid.find_all_hid_devices():
        if dev.vendor_id != SONY_VID or dev.product_id not in wanted:
            continue
        out_len = 0
        usage_page = usage = 0
        try:
            dev.open()
            caps = dev.hid_caps
            out_len = int(caps.output_report_byte_length)
            usage_page = int(caps.usage_page)
            usage = int(caps.usage)
        except Exception:  # noqa: BLE001 - 被独占占用时拿不到 caps 也没关系
            pass
        finally:
            try:
                dev.close()
            except Exception:  # noqa: BLE001
                pass
        found.append(
            Ds4HidDevice(
                path=dev.device_path,
                vendor_id=dev.vendor_id,
                product_id=dev.product_id,
                product_name=dev.product_name or "",
                manufacturer=dev.vendor_name or "",
                output_report_length=out_len,
                usage_page=usage_page,
                usage=usage,
            )
        )
    # 蓝牙优先，其次按 PID
    found.sort(key=lambda d: (not d.is_bluetooth, d.product_id, d.path))
    return found


_kernel32 = None


def _k32():
    global _kernel32
    if _kernel32 is None:
        if sys.platform != "win32":
            raise RuntimeError("蓝牙 HID 推流目前只支持 Windows")
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.restype = ctypes.c_void_p
        k.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        ]
        k.WriteFile.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
        ]
        k.WriteFile.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        k.CloseHandle.restype = wintypes.BOOL
        _kernel32 = k
    return _kernel32


class HidWriter:
    """对着手柄 HID 接口写原始报告（WriteFile，支持超过 64 字节的厂商报告）。"""

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    OPEN_EXISTING = 3

    def __init__(self, path: str, exclusive: bool = False):
        self.path = path
        self.exclusive = exclusive
        self._handle: Optional[int] = None
        self.max_write = 0

    @property
    def is_open(self) -> bool:
        return bool(self._handle)

    def open(self) -> None:
        k32 = _k32()
        share = 0 if self.exclusive else (self.FILE_SHARE_READ | self.FILE_SHARE_WRITE)
        handle = k32.CreateFileW(
            self.path,
            self.GENERIC_READ | self.GENERIC_WRITE,
            share, None, self.OPEN_EXISTING, 0, None,
        )
        if not handle or handle == ctypes.c_void_p(-1).value:
            err = ctypes.get_last_error()
            raise OSError(err, "打开 HID 设备失败（%s）：%s" % (err, os.strerror(err)))
        self._handle = handle

    def write(self, data: bytes) -> int:
        if not self._handle:
            raise RuntimeError("HID 设备尚未打开")
        k32 = _k32()
        buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        written = wintypes.DWORD(0)
        ok = k32.WriteFile(self._handle, buf, len(data), ctypes.byref(written), None)
        if not ok:
            err = ctypes.get_last_error()
            raise OSError(err, "写入 HID 报告失败（%s）：%s" % (err, os.strerror(err)))
        return int(written.value)

    def close(self) -> None:
        if self._handle:
            try:
                _k32().CloseHandle(self._handle)
            except Exception:  # noqa: BLE001
                pass
            self._handle = None

    def __enter__(self) -> "HidWriter":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# --------------------------------------------------------------------------- #
# SBC 编码（ffmpeg）与帧解析
# --------------------------------------------------------------------------- #

@dataclass
class SbcParams:
    samplerate: int
    subbands: int
    blocks: int
    mode: str
    allocation: str
    bitpool: int
    frame_length: int

    def describe(self) -> str:
        return ("%d Hz / %d 子带 / %d 块 / %s / %s / 比特池 %d / 帧长 %d 字节"
                % (self.samplerate, self.subbands, self.blocks, self.mode,
                   self.allocation, self.bitpool, self.frame_length))


def sbc_frame_length_from_header(header: bytes) -> int:
    """按 SBC 帧头算出该帧字节长度（与 ffmpeg/libsbc 的实现一致）。

    ``frame_length = 4 + (4·subbands·channels)/8
                     + ceil((blocks·bitpool·(1+dual) + joint·subbands)/8)``
    """
    if len(header) < 4:
        raise ValueError("SBC 帧头至少 4 字节")
    if header[0] != 0x9C:
        raise ValueError("SBC 同步字错误：0x%02X" % header[0])
    b1 = header[1]
    bitpool = header[2]
    subbands = 8 if (b1 & 0x01) else 4
    blocks = _BLOCKS_TABLE[(b1 >> 4) & 0x03]
    mode = (b1 >> 2) & 0x03
    channels = 1 if mode == 0 else 2
    dual = 1 if mode == 1 else 0
    joint = 1 if mode == 3 else 0
    body = blocks * bitpool * (1 + dual) + joint * subbands
    return 4 + (4 * subbands * channels) // 8 + (body + 7) // 8


def parse_sbc_params(frame: bytes) -> SbcParams:
    b1 = frame[1]
    mode = (b1 >> 2) & 0x03
    subbands = 8 if (b1 & 0x01) else 4
    return SbcParams(
        samplerate=_FREQ_TABLE[b1 >> 6],
        subbands=subbands,
        blocks=_BLOCKS_TABLE[(b1 >> 4) & 0x03],
        mode=_MODE_NAMES[mode],
        allocation="SNR" if (b1 >> 1) & 1 else "LOUDNESS",
        bitpool=frame[2],
        frame_length=sbc_frame_length_from_header(frame),
    )


def split_sbc_frames(data: bytes) -> List[bytes]:
    """把裸 SBC 流切成一个个完整帧。"""
    frames: List[bytes] = []
    pos = 0
    total = len(data)
    while pos + 4 <= total:
        if data[pos] != 0x9C:
            # 容错：向后找同步字
            nxt = data.find(b"\x9c", pos + 1)
            if nxt < 0:
                break
            pos = nxt
            continue
        length = sbc_frame_length_from_header(data[pos:pos + 4])
        if pos + length > total:
            break
        frames.append(data[pos:pos + length])
        pos += length
    return frames


def _ffmpeg_cmd(input_args: Sequence[str], bitrate: int, delay: str, filters: Sequence[str]) -> List[str]:
    exe = find_ffmpeg()
    if not exe:
        raise AudioError("未找到 ffmpeg，无法进行 SBC 编码")
    cmd = [exe, "-hide_banner", "-v", "error", "-nostdin"]
    cmd += list(input_args)
    if filters:
        cmd += ["-af", ",".join(filters)]
    cmd += ["-ac", "2", "-ar", str(SBC_SAMPLE_RATE), "-c:a", "sbc"]
    if bitrate:
        cmd += ["-b:a", str(int(bitrate))]
    if delay:
        cmd += ["-sbc_delay", str(delay)]
    cmd += ["-f", "sbc", "-"]
    return cmd


def _run_ffmpeg(cmd: List[str], stdin_data: Optional[bytes] = None) -> bytes:
    flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
    proc = subprocess.run(cmd, input=stdin_data, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, creationflags=flags, check=False)
    if proc.returncode != 0 or not proc.stdout:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise AudioError("SBC 编码失败：%s" % (" | ".join(err[-3:]) if err else "未知错误"))
    return proc.stdout


def _encode_with_target(
    cmd_args: Sequence[str], delay: str, bitrate: int, filters: Sequence[str],
    stdin_data: Optional[bytes], target_frame_length: int,
) -> Tuple[List[bytes], SbcParams]:
    """跑 ffmpeg 编码；若帧长不是手柄要求的长度，就按公式换比特率再编一次。"""
    def run(br: int) -> Tuple[List[bytes], SbcParams]:
        data = _run_ffmpeg(_ffmpeg_cmd(cmd_args, br, delay, filters), stdin_data=stdin_data)
        frames = split_sbc_frames(data)
        if not frames:
            raise AudioError("ffmpeg 没有输出任何 SBC 帧（音频可能为空）")
        return frames, parse_sbc_params(frames[0])

    frames, params = run(bitrate)
    if not target_frame_length or params.frame_length == target_frame_length:
        return frames, params

    frames, params = run(bitrate_for_bitpool(bitpool_for_frame_length(target_frame_length)))
    if params.frame_length != target_frame_length:
        raise AudioError(
            "无法生成 %d 字节的 SBC 帧（当前 %d 字节）：手柄按固定帧长解析，帧长不对就完全没声音"
            % (target_frame_length, params.frame_length))
    return frames, params


def encode_sbc_file(
    path: str, bitrate: int = SBC_DEFAULT_BITRATE, delay: str = SBC_DEFAULT_DELAY,
    gain_db: float = 0.0, channel: str = "both", start: float = 0.0, duration: float = 0.0,
    target_frame_length: int = 0,
) -> Tuple[List[bytes], SbcParams]:
    """把任意音频文件编码成 SBC 帧（重采样/混音/裁剪都交给 ffmpeg）。"""
    if not os.path.isfile(path):
        raise AudioError("文件不存在: %s" % path)
    args = ["-i", path]
    filters: List[str] = []
    if gain_db:
        filters.append("volume=%.2fdB" % gain_db)
    if channel == "left":
        filters.append("pan=stereo|c0=c0|c1=0")
    elif channel == "right":
        filters.append("pan=stereo|c0=0|c1=c1")
    if start > 0:
        filters.append("atrim=start=%.3f" % start)
    if duration > 0:
        filters.append("atrim=duration=%.3f" % duration)
    return _encode_with_target(args, delay, bitrate, filters, None, target_frame_length)


def encode_sbc_pcm(
    samples: np.ndarray, bitrate: int = SBC_DEFAULT_BITRATE, delay: str = SBC_DEFAULT_DELAY,
    gain_db: float = 0.0, channel: str = "both",
    target_frame_length: int = 0,
) -> Tuple[List[bytes], SbcParams]:
    """把内存里的 float32 音频（32 kHz 立体声）编码成 SBC 帧。"""
    x = np.asarray(samples, dtype=np.float32)
    if x.ndim == 1:
        x = x[:, None]
    if x.shape[1] == 1:
        x = np.repeat(x, 2, axis=1)
    elif x.shape[1] > 2:
        x = x[:, :2]
    if gain_db:
        x = x * (10.0 ** (gain_db / 20.0))
    if channel == "left":
        x = np.stack([x[:, 0], np.zeros_like(x[:, 0])], axis=1)
    elif channel == "right":
        x = np.stack([np.zeros_like(x[:, 1]), x[:, 1]], axis=1)
    pcm = (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    args = ["-f", "s16le", "-ar", str(SBC_SAMPLE_RATE), "-ac", "2", "-i", "-"]
    return _encode_with_target(args, delay, bitrate, [], pcm, target_frame_length)


def decode_sbc_frames(frames: Sequence[bytes]) -> np.ndarray:
    """用 ffmpeg 把 SBC 帧解回 PCM（自检用）。"""
    exe = find_ffmpeg()
    if not exe:
        raise AudioError("未找到 ffmpeg")
    raw = b"".join(frames)
    flags = 0x08000000 if sys.platform == "win32" else 0
    proc = subprocess.run(
        [exe, "-hide_banner", "-v", "error", "-f", "sbc", "-i", "-",
         "-f", "s16le", "-ac", "2", "-ar", str(SBC_SAMPLE_RATE), "-"],
        input=raw, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=flags, check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise AudioError("SBC 解码失败: %s" % err)
    pcm = np.frombuffer(proc.stdout, "<i2").astype(np.float32) / 32768.0
    return pcm.reshape(-1, 2)


# --------------------------------------------------------------------------- #
# 报告打包
# --------------------------------------------------------------------------- #

def build_control_report(
    volume: int = 255, led: Tuple[int, int, int] = (0, 255, 0),
    rumble: Tuple[int, int] = (0, 0), write_size: int = 0,
) -> bytes:
    """0x11 控制报告 —— **必须先发这条，内置喇叭才有声音**。

    布局来自 PS4 真机抓包（SensePost 2020）+ Linux hid-playstation.c + Unity 实现互证：

        [0]=0x11  [1]=0xC0(hw_control)  [2]=0x20(**audio_control，启用音频路径**)
        [3]=0xF3  [4]=0x44  [5]=0x00  [6]=motor_right  [7]=motor_left
        [8..10]=灯条 RGB  [11..12]=闪烁
        [21..25]=0x43,0x43,0x00,音量%(0-100),0x85
        [74..77]=CRC32（种子 0xA2，覆盖前 74 字节，小端）

    两个曾经踩过的坑：``[2]`` 必须是 **0x20**（0xA2 只是音频报告的第 3 字节）；
    音量是**百分比**且后面还要跟一个 0x85。``write_size`` 非 0 时把报告补零写满
    （Windows 按集合最大长度发送，实测必须写满 547 字节）。
    """
    size = max(int(write_size or 0), CTRL_REPORT_SIZE)
    rep = bytearray(size)
    rep[0] = CTRL_REPORT_ID
    rep[1] = 0xC0
    rep[2] = 0x20
    rep[3] = 0xF3
    rep[4] = 0x44
    rep[5] = 0x00
    rep[6] = int(rumble[0]) & 0xFF
    rep[7] = int(rumble[1]) & 0xFF
    rep[8] = int(led[0]) & 0xFF
    rep[9] = int(led[1]) & 0xFF
    rep[10] = int(led[2]) & 0xFF
    rep[11] = 0x00
    rep[12] = 0x00
    pct = max(0, min(100, int(round(int(volume) * 100 / 255.0))))
    rep[21] = 0x43
    rep[22] = 0x43
    rep[23] = 0x00
    rep[24] = pct
    rep[25] = 0x85
    rep[CTRL_REPORT_SIZE - 4:CTRL_REPORT_SIZE] = crc32_bt(
        bytes(rep[:CTRL_REPORT_SIZE - 4])).to_bytes(4, "little")
    return bytes(rep)


def build_raw_audio_report(
    report_id: int, size: int, frames: Sequence[bytes], counter: int,
    output_mode: int = OUTPUT_DEFAULT, crc_with_bt_header: bool = True,
    byte2: int = AUDIO_BYTE2, write_size: int = 0, byte1: int = 0x40,
) -> bytes:
    """打包一条音频报告。

    ``size`` 是这条报告的**真实长度**（0x17=462 / 0x14=270），CRC 就放在它的末尾；
    ``write_size`` 非 0 时把整条报告补零写满（Windows 按集合最大长度 547 发送）。
    ``byte2`` 是第 3 字节（音频报告为 0xA0；控制报告 0x11 才是 0x20）。
    """
    if not frames:
        raise ValueError("至少要有一个 SBC 帧")
    payload_size = sum(len(f) for f in frames)
    if 6 + payload_size + 4 > size:
        raise ValueError("SBC 帧总长 %d 放不进 %d 字节的报告" % (payload_size, size))
    total = max(int(write_size or 0), size)
    buf = bytearray(total)
    buf[0] = report_id & 0xFF
    buf[1] = byte1 & 0xFF
    buf[2] = byte2 & 0xFF
    buf[3] = counter & 0xFF
    buf[4] = (counter >> 8) & 0xFF
    buf[5] = output_mode & 0xFF
    pos = 6
    for frame in frames:
        buf[pos:pos + len(frame)] = frame
        pos += len(frame)
    buf[size - 4:size] = crc32_bt(bytes(buf[:size - 4])).to_bytes(4, "little")
    return bytes(buf)


def build_audio_report(
    frames: Sequence[bytes], counter: int, output_mode: int = OUTPUT_DEFAULT,
    crc_with_bt_header: bool = True, force_two_frames: bool = False,
) -> bytes:
    """构造音频报告（默认 0x14 / 2 帧 / 270 字节 —— 实测唯一能让喇叭出声的形式）。"""
    if not frames:
        raise ValueError("至少要有一个 SBC 帧")
    if force_two_frames or len(frames) < 4:
        report_id, size = AUDIO_REPORT_ID_2, AUDIO_REPORT_SIZE_2
    else:
        report_id, size = AUDIO_REPORT_ID_4, AUDIO_REPORT_SIZE_4
    return build_raw_audio_report(report_id, size, frames, counter, output_mode,
                                  crc_with_bt_header, AUDIO_BYTE2)


# --------------------------------------------------------------------------- #
# 推流
# --------------------------------------------------------------------------- #

def _begin_timer_resolution() -> bool:
    """把 Windows 定时器精度提到 1 ms（否则 time.sleep 会有 ~15 ms 抖动）。"""
    if sys.platform != "win32":
        return False
    try:
        ctypes.WinDLL("winmm").timeBeginPeriod(1)
        return True
    except Exception:  # noqa: BLE001
        return False


def _end_timer_resolution(enabled: bool) -> None:
    if not enabled:
        return
    try:
        ctypes.WinDLL("winmm").timeEndPeriod(1)
    except Exception:  # noqa: BLE001
        pass


def _sleep_until(target: float) -> None:
    """睡到指定时刻（最后 0.5 ms 自旋，保证 16 ms 级别的精度）。"""
    while True:
        remaining = target - time.perf_counter()
        if remaining <= 0:
            return
        if remaining > 0.0005:
            time.sleep(remaining - 0.0004)
        else:
            return


@dataclass
class StreamResult:
    frames_sent: int = 0
    reports_sent: int = 0
    bytes_sent: int = 0
    write_errors: int = 0
    late_reports: int = 0
    max_late_ms: float = 0.0
    control_reports: int = 0
    seconds: float = 0.0
    report_size_used: int = 0
    stopped: bool = False

    @property
    def audio_seconds(self) -> float:
        return self.frames_sent / SBC_FRAMES_PER_SECOND


class Ds4BtSpeaker:
    """通过蓝牙把 SBC 音频推给 DS4 的内置喇叭 / 耳机口。"""

    def __init__(
        self,
        device: Ds4HidDevice,
        output_mode: int = OUTPUT_DEFAULT,
        frames_per_report: int = 4,
        exclusive: bool = False,
        volume: int = 255,
        led: Tuple[int, int, int] = (0, 255, 0),
        report_id: int = 0,
        report_size: int = 0,
        byte2: int = AUDIO_BYTE2,
        write_size: int = 0,
        lead_reports: int = 6,
        speed: float = 1.0,
        control_every: int = 20,
        log: Optional[Callable[[str], None]] = None,
    ):
        self.device = device
        self.output_mode = int(output_mode) & 0xFF
        self.frames_per_report = max(1, min(4, int(frames_per_report)))
        self.exclusive = exclusive
        self.volume = max(0, min(255, int(volume)))
        self.led = tuple(int(v) & 0xFF for v in led)  # type: ignore[assignment]
        # report_id=0 表示自动：4 帧用 0x17/462 字节，其余用 0x14/270 字节
        self.report_id = int(report_id) & 0xFF
        self.report_size = int(report_size)
        self.byte2 = int(byte2) & 0xFF
        # Windows 按 HID 描述符的集合最大长度发送，实测必须写满（本机 547 字节）
        self.write_size = int(write_size or getattr(device, "output_report_length", 0)
                              or DEFAULT_WRITE_SIZE)
        # 预灌缓冲：一开始连发若干条报告，把手柄的抖动缓冲填起来，避免周期性欠载导致卡顿
        self.lead_reports = max(0, int(lead_reports))
        # 送流速率倍数：1.0 = 严格实时；略大于 1 可以慢慢把缓冲垫厚
        self.speed = float(speed) if speed else 1.0
        # 每 N 条音频报告插一条 0x11 控制报告（PS4 真机就是把控制状态混在音频流里持续刷新的；
        # 完全不刷新时手柄音频路径偶尔会"饿"，表现为断续）。0 = 关闭
        self.control_every = max(0, int(control_every))
        self.log = log or (lambda _m: None)
        self.writer = HidWriter(device.path, exclusive=exclusive)
        self.result = StreamResult()

    def describe(self) -> str:
        rid = "自动(0x17/0x14)" if not self.report_id else "0x%02X/%d 字节" % (
            self.report_id, self.report_size)
        return ("报告 %s / 写入 %d 字节 / 第3字节 0x%02X / 通路 0x%02X / 音量 %d / "
                "灯条 %s / 预灌 %d 条 / 速率 x%.2f / 控制刷新 %s" % (
                    rid, self.write_size, self.byte2, self.output_mode, self.volume,
                    self.led, self.lead_reports, self.speed,
                    ("每 %d 条" % self.control_every) if self.control_every else "关闭"))

    # ---------------------------------------------------------------- 设备
    def open(self) -> None:
        if not self.device.is_bluetooth:
            raise RuntimeError(
                "手柄当前是 %s 连接，内置扬声器只能通过**蓝牙**驱动。"
                "请用手柄的 Share + PS 键配对电脑（或拔掉 USB 线改用蓝牙），再重试。"
                % self.device.transport
            )
        self.writer.open()

    def close(self) -> None:
        self.writer.close()

    def send_control_report(self, rumble: Tuple[int, int] = (0, 0)) -> None:
        """发 0x11 控制报告：启用音频路径 + 设置扬声器音量 + 灯条。

        ``rumble`` 非零时手柄会震一下 —— 这是确认 HID 通道打通的直观信号。
        """
        self.writer.write(build_control_report(self.volume, self.led, rumble,
                                               write_size=self.write_size))

    # ---------------------------------------------------------------- 推流
    def play(
        self,
        frames: Sequence[bytes],
        realtime: bool = True,
        stop_flag: Optional[threading.Event] = None,
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> StreamResult:
        """按实时节奏把 SBC 帧推给手柄。"""
        result = StreamResult()
        if not frames:
            return result

        total = len(frames)
        # 先把全部报告打包好：推流期间只做"等待 + 写入"，把 Python 侧抖动与 GC 降到最低
        plan: List[Tuple[bytes, int]] = []
        index = 0
        counter = 0
        while index < total:
            if self.report_id:
                rid, size = self.report_id, self.report_size
                take = min(self.frames_per_report, total - index)
            else:
                remaining = total - index
                if remaining >= 4:
                    rid, size, take = AUDIO_REPORT_ID_4, AUDIO_REPORT_SIZE_4, 4
                elif remaining >= 2:
                    rid, size, take = AUDIO_REPORT_ID_2, AUDIO_REPORT_SIZE_2, 2
                else:
                    rid, size, take = AUDIO_REPORT_ID_2, AUDIO_REPORT_SIZE_2, 1
            plan.append((
                build_raw_audio_report(rid, size, frames[index:index + take], counter,
                                       self.output_mode, True, self.byte2,
                                       write_size=self.write_size),
                take,
            ))
            counter = (counter + take) & 0xFFFF
            index += take

        # 1) 先发一版带马达的 0x11：手柄震一下 = 通道通、音频路径已启用
        self.send_control_report((0, 60))
        time.sleep(0.25)
        # 2) 正式 0x11：关马达、灯条变色、设置扬声器音量
        self.send_control_report((0, 0))
        time.sleep(0.05)

        lead_frames = self.lead_reports * max(1, self.frames_per_report)
        rate = SBC_FRAMES_PER_SECOND * (self.speed if self.speed > 0 else 1.0)
        sent = 0
        started = time.perf_counter()
        timer_up = _begin_timer_resolution()
        try:
            for report, nframes in plan:
                if stop_flag is not None and stop_flag.is_set():
                    result.stopped = True
                    break
                if realtime:
                    target = started + (sent - lead_frames) / rate
                    now = time.perf_counter()
                    if target > now:
                        _sleep_until(target)
                    elif sent >= lead_frames:
                        late = (now - target) * 1000.0
                        if late > 1.0:
                            result.late_reports += 1
                            result.max_late_ms = max(result.max_late_ms, late)
                written = self.writer.write(report)
                result.frames_sent += nframes
                result.reports_sent += 1
                result.bytes_sent += written
                result.report_size_used = len(report)
                sent += nframes
                if self.control_every and result.reports_sent % self.control_every == 0:
                    # 周期性刷新控制状态（不发马达，避免手感干扰）
                    self.send_control_report((0, 0))
                    result.control_reports += 1
                if progress is not None:
                    progress(result.frames_sent, total)
        finally:
            _end_timer_resolution(timer_up)

        result.seconds = time.perf_counter() - started
        self.result = result
        return result


def pick_bt_device(
    devices: Optional[Sequence[Ds4HidDevice]] = None, require_bt: bool = True
) -> Optional[Ds4HidDevice]:
    devs = list(devices) if devices is not None else find_ds4_hid_devices()
    if require_bt:
        devs = [d for d in devs if d.is_bluetooth]
    if not devs:
        return None
    # 优先 0x09CC/0x05C4
    devs.sort(key=lambda d: (d.product_id not in DS4_PIDS, d.output_report_length))
    return devs[0]


def format_hid_list(devices: Optional[Sequence[Ds4HidDevice]] = None) -> str:
    devs = list(devices) if devices is not None else find_ds4_hid_devices()
    if not devs:
        return "没有找到任何 PlayStation 手柄 HID 接口（请连接手柄后重试）。"
    lines = ["找到 %d 个手柄 HID 接口：" % len(devs)]
    for d in devs:
        lines.append("  " + d.label + ("   ← 可用于内置扬声器" if d.is_bluetooth else "   （USB：内置扬声器不可用）"))
        lines.append("      " + d.path)
    return "\n".join(lines)
