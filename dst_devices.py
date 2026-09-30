# -*- coding: utf-8 -*-
"""音频设备枚举与 PlayStation 手柄（DS4 / DualSense）端点自动识别。

Windows 下手柄音频端点的常见命名（中文系统）：

* USB 连接：``耳机 (4- Wireless Controller)``          → 手柄扬声器 / 耳机口
            ``麦克风 (4- Wireless Controller)``        → 手柄麦克风
* 蓝牙连接：``耳机 (Wireless Controller)``              → A2DP 输出
            ``头戴式受话器 (Wireless Controller)``     → HFP（麦克风 / 单声道输出）

需要注意：MME 主机 API 会把端点名截断到 31 个字符，因此自动选择时优先
WASAPI / DirectSound 端点；找不到手柄端点时再退回默认设备。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

import sounddevice as sd

#: 匹配 PlayStation 手柄音频端点的关键字（不区分大小写）
DS4_PATTERNS: Sequence[str] = (
    r"wireless\s*controller",
    r"dualshock",
    r"dual\s*shock",
    r"dualsense",
    r"dual\s*sense",
    r"\bds4\b",
    r"\bdse\b",
    r"playstation",
    r"\bps4\b",
    r"\bps5\b",
    r"\bsony\b",
    r"无线控制器",
    r"无线手柄",
)
_DS4_RE = re.compile("|".join(DS4_PATTERNS), re.IGNORECASE)


def looks_like_ds4(name: str) -> bool:
    """端点名是否像 PlayStation 手柄音频。"""
    return bool(_DS4_RE.search(name or ""))


def _hostapi_rank(name: str) -> int:
    """主机 API 优先级，越小越优先（WASAPI 抖动最小，MME 仅作兜底）。"""
    n = (name or "").lower()
    if "wasapi" in n:
        return 0
    if "directsound" in n:
        return 1
    if "wdm" in n:
        return 2
    if "asio" in n:
        return 3
    if "mme" in n:
        return 4
    return 5


def endpoint_key(name: str) -> str:
    """提取端点"物理设备"标识，用于把输出和输入配对到同一个手柄。

    ``耳机 (4- Wireless Controller)`` 与 ``麦克风 (4- Wireless Controller)``
    会得到同一个 key，而蓝牙手柄（没有 ``4-`` 前缀）会得到另一个 key。
    """
    text = (name or "").strip()
    idx = text.find("(")
    if idx >= 0:
        text = text[idx:]
    return text.strip().lower()


@dataclass(frozen=True)
class AudioDevice:
    """一个 PortAudio 设备的只读快照。"""

    index: int
    name: str
    hostapi: int
    hostapi_name: str
    max_input_channels: int
    max_output_channels: int
    default_samplerate: float
    is_default: bool = False

    @property
    def is_ds4(self) -> bool:
        return looks_like_ds4(self.name)

    @property
    def key(self) -> str:
        return endpoint_key(self.name)

    @property
    def rank(self) -> int:
        return _hostapi_rank(self.hostapi_name)

    @property
    def channels(self) -> int:
        return self.max_output_channels or self.max_input_channels

    @property
    def label(self) -> str:
        kind = "输出" if self.max_output_channels else "输入"
        return "[%d] %s · %s · %s · %d ch · %d Hz%s" % (
            self.index, self.name, self.hostapi_name, kind,
            self.channels, int(self.default_samplerate),
            " · 系统默认" if self.is_default else "",
        )

    def __str__(self) -> str:  # pragma: no cover - 仅用于打印
        return self.label


def _default_indices() -> Dict[str, Optional[int]]:
    out: Dict[str, Optional[int]] = {"input": None, "output": None}
    for kind in ("input", "output"):
        try:
            out[kind] = int(sd.default.device[0 if kind == "input" else 1])
            if out[kind] is not None and out[kind] < 0:
                out[kind] = None
        except Exception:  # noqa: BLE001 - 没有默认设备时忽略
            out[kind] = None
    return out


def list_devices(kind: str = "output") -> List[AudioDevice]:
    """列出所有可用设备（kind: 'output' / 'input' / 'all'）。"""
    want_out = kind in ("output", "all")
    want_in = kind in ("input", "all")
    try:
        raw = sd.query_devices()
        hostapis = sd.query_hostapis()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("查询音频设备失败: %s" % exc) from exc

    defaults = _default_indices()
    devices: List[AudioDevice] = []
    for idx, dev in enumerate(raw):
        max_out = int(dev.get("max_output_channels", 0))
        max_in = int(dev.get("max_input_channels", 0))
        if max_out <= 0 and max_in <= 0:
            continue
        if max_out > 0 and not want_out and not want_in:
            continue
        if want_out and not want_in and max_out <= 0:
            continue
        if want_in and not want_out and max_in <= 0:
            continue
        hostapi = int(dev.get("hostapi", 0))
        hostapi_name = hostapis[hostapi]["name"] if hostapi < len(hostapis) else "?"
        is_default = idx in (defaults["output"], defaults["input"])
        devices.append(
            AudioDevice(
                index=idx,
                name=str(dev.get("name", "")),
                hostapi=hostapi,
                hostapi_name=hostapi_name,
                max_input_channels=max_in,
                max_output_channels=max_out,
                default_samplerate=float(dev.get("default_samplerate", 48000.0) or 48000.0),
                is_default=is_default,
            )
        )
    devices.sort(key=lambda d: (d.rank, d.index))
    return devices


def ds4_devices(kind: str = "output") -> List[AudioDevice]:
    """只返回疑似手柄的音频端点。"""
    return [d for d in list_devices(kind) if d.is_ds4]


def pick_output(devices: Optional[Sequence[AudioDevice]] = None) -> Optional[AudioDevice]:
    """自动挑选手柄扬声器端点；没有手柄时退回系统默认输出。"""
    devs = list(devices) if devices is not None else list_devices("output")
    outs = [d for d in devs if d.max_output_channels > 0]
    if not outs:
        return None
    ds4 = [d for d in outs if d.is_ds4]
    if ds4:
        # 优先"名字更短"的端点：蓝牙 A2DP 端点通常同时能驱动手柄喇叭与耳机口
        return sorted(ds4, key=lambda d: (d.rank, len(d.key), d.index))[0]
    defaults = [d for d in outs if d.is_default]
    if defaults:
        return sorted(defaults, key=lambda d: d.rank)[0]
    return outs[0]


def pick_input(
    devices: Optional[Sequence[AudioDevice]] = None,
    peer: Optional[AudioDevice] = None,
    prefer_peer: bool = False,
) -> Optional[AudioDevice]:
    """自动挑选录音设备。

    注意：**DS4（DualShock 4）本身没有内置麦克风**，Windows 里那个
    ``麦克风 (…Wireless Controller)`` 是 3.5mm 耳机口上的耳麦麦克风，只有插了
    带麦耳机才有信号（DualSense / PS5 手柄才有内置麦克风阵列）。
    所以这里默认优先"真正的麦克风"（系统默认输入、且不是手柄端点），
    只有当用户显式要求 ``prefer_peer=True`` 时才优先与输出配对的手柄端点。
    """
    devs = list(devices) if devices is not None else list_devices("input")
    ins = [d for d in devs if d.max_input_channels > 0]
    if not ins:
        return None

    if prefer_peer and peer is not None:
        same_key = [d for d in ins if d.key == peer.key]
        if same_key:
            return sorted(same_key, key=lambda d: (d.rank, d.index))[0]

    defaults = [d for d in ins if d.is_default and not d.is_ds4]
    if defaults:
        return sorted(defaults, key=lambda d: d.rank)[0]

    others = [d for d in ins if not d.is_ds4]
    if others:
        return sorted(others, key=lambda d: (not d.is_default, d.rank, d.index))[0]

    return sorted(ins, key=lambda d: (d.rank, d.index))[0]


def match_device(spec: Optional[str], kind: str = "output") -> Optional[AudioDevice]:
    """按序号或名称子串解析设备。

    spec 可以是 ``"46"``（PortAudio 序号）、``"Wireless Controller"``（唯一子串），
    或 ``None`` / ``"auto"``（自动识别手柄）。
    """
    devs = list_devices(kind)
    if kind == "output":
        devs = [d for d in devs if d.max_output_channels > 0]
    else:
        devs = [d for d in devs if d.max_input_channels > 0]

    if spec is None or str(spec).strip().lower() in ("", "auto", "default"):
        return pick_output(devs) if kind == "output" else pick_input(devs)

    text = str(spec).strip()
    if text.isdigit():
        idx = int(text)
        for d in devs:
            if d.index == idx:
                return d
        raise ValueError("设备序号 %d 不是可用的%s设备" % (idx, "输出" if kind == "output" else "输入"))

    low = text.lower()
    hits = [d for d in devs if low in d.name.lower()]
    if not hits:
        raise ValueError("没有名称包含 %r 的%s设备" % (spec, "输出" if kind == "output" else "输入"))
    hits.sort(key=lambda d: (not d.is_ds4, d.rank, d.index))
    return hits[0]


def format_device_list(kind: str = "output") -> str:
    """人类可读的设备清单（用于 --list）。"""
    devs = list_devices(kind)
    lines: List[str] = []
    if kind in ("output", "all"):
        outs = [d for d in devs if d.max_output_channels > 0]
        lines.append("可用的音频输出设备（%d 个）：" % len(outs))
        for d in outs:
            lines.append("  " + d.label + ("   ← 手柄扬声器 / 耳机口" if d.is_ds4 else ""))
    if kind in ("input", "all"):
        ins = [d for d in devs if d.max_input_channels > 0]
        lines.append("")
        lines.append("可用的音频输入设备（%d 个）：" % len(ins))
        for d in ins:
            lines.append("  " + d.label + (
                "   ← 手柄耳机口耳麦（DS4 无内置麦克风，需插带麦耳机）" if d.is_ds4 else ""))
    return "\n".join(lines)
