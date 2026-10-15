# -*- coding: utf-8 -*-
"""极简双语（简体中文 / English）支持。

设计要点：
* 以**中文原文**作为 key（gettext 风格），找不到英文条目时原样返回中文，绝不抛异常；
* 语言可来自 ``--lang``、环境变量 ``DS4_LANG``、或系统界面语言自动判断；
* 图形界面切换语言后会立即重刷已注册的控件文本（见 dst_gui 的 ``_L`` 辅助）。
"""

from __future__ import annotations

import os
import sys
from typing import Dict, Optional

LANG_ZH = "zh"
LANG_EN = "en"
LANG_NAMES = {LANG_ZH: "简体中文", LANG_EN: "English"}

_current: str = LANG_ZH


def normalize(lang: Optional[str]) -> Optional[str]:
    """把各种写法归一到 'zh' / 'en'；无法识别返回 None。"""
    if not lang:
        return None
    text = str(lang).strip().lower().replace("_", "-")
    if text in ("zh", "zh-cn", "zh-hans", "cn", "chinese", "schinese", "中文", "简体中文"):
        return LANG_ZH
    if text in ("en", "en-us", "en-gb", "english", "eng", "英文", "英语"):
        return LANG_EN
    return None


def detect_system_lang() -> str:
    """按系统界面语言猜一个默认值。"""
    try:
        if sys.platform == "win32":
            import ctypes

            lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            # 0x0804 简体中文 / 0x0404 繁体中文 / 0x0C04 香港
            if (lang_id & 0x3FF) == 0x04:
                return LANG_ZH
            return LANG_EN
    except Exception:  # noqa: BLE001
        pass
    try:
        import locale

        code = (locale.getlocale()[0] or "") + (os.environ.get("LANG") or "")
        return LANG_ZH if "zh" in code.lower() or "chinese" in code.lower() else LANG_EN
    except Exception:  # noqa: BLE001
        return LANG_ZH


def set_lang(lang: Optional[str], fallback: bool = True) -> str:
    """设置当前语言；无法识别时依次回退：环境变量 → 已保存设置 → 系统语言。"""
    global _current
    code = normalize(lang)
    if code is None:
        code = normalize(os.environ.get("DS4_LANG"))
    if code is None:
        code = normalize(load_saved_lang())
    if code is None:
        code = detect_system_lang() if fallback else _current
    _current = code
    return _current


def settings_path() -> str:
    """语言等偏好设置的存放位置（Windows: %APPDATA%\\DS4-Speaker-Test\\settings.json）。"""
    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~")
    return os.path.join(base, "DS4-Speaker-Test", "settings.json")


def load_saved_lang() -> Optional[str]:
    try:
        import json

        with open(settings_path(), "r", encoding="utf-8") as fh:
            return normalize(json.load(fh).get("lang"))
    except Exception:  # noqa: BLE001
        return None


def save_lang(lang: str) -> bool:
    """把语言写进设置文件（失败也不抛异常）。"""
    code = normalize(lang)
    if code is None:
        return False
    try:
        import json

        path = settings_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh) or {}
            except Exception:  # noqa: BLE001
                data = {}
        data["lang"] = code
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        return True
    except Exception:  # noqa: BLE001
        return False


def current_lang() -> str:
    return _current


def lang_name(lang: Optional[str] = None) -> str:
    return LANG_NAMES.get(lang or _current, _current)


def other_lang() -> str:
    return LANG_EN if _current == LANG_ZH else LANG_ZH


def t(text: str, *args, **kwargs) -> str:
    """取当前语言下的文本；``text`` 就是中文原文。"""
    table: Dict[str, str] = STRINGS.get(_current, {})
    out = table.get(text)
    if out is None:
        out = STRINGS[LANG_EN].get(text, text) if _current == LANG_EN else text
    if args or kwargs:
        try:
            return out.format(*args, **kwargs)
        except Exception:  # noqa: BLE001 - 占位符不匹配时退回原文
            return out
    return out


# --------------------------------------------------------------------------- #
# 英文词条：key = 中文原文
# --------------------------------------------------------------------------- #

STRINGS: Dict[str, Dict[str, str]] = {
    LANG_ZH: {},
    LANG_EN: {
        # ---------- 通用 ----------
        "内置测试信号：": "Built-in test signals: ",
        "DS4 / DualSense 手柄扬声器测试工具（支持自定义测试音频）":
            "DS4 / DualSense controller speaker test tool (custom test audio supported)",
        "就绪": "Ready",
        "已停止": "Stopped",
        "错误": "Error",
        "警告": "Warning",
        "提示": "Note",
        "说明": "Note",
        "跳过": "Skipped",
        "通过": "PASS",
        "失败": "FAIL",
        "已完成": "Done",
        "音频": "Audio",
        "设备": "Device",
        "音量": "Volume",
        "频率(Hz)": "Frequency (Hz)",
        "时长(秒, 0=默认)": "Duration (s, 0=default)",
        "起始(秒)": "Start (s)",
        "声道路由": "Channel routing",
        "循环播放": "Loop",
        "开始播放": "Play",
        "停止": "Stop",
        "完整测试序列": "Full test sequence",
        "灯条/震动自检": "Lightbar / rumble check",
        "浏览...": "Browse...",
        "刷新设备": "Refresh devices",
        "只显示手柄端点": "Controller endpoints only",
        "保存录音为 WAV": "Save recording as WAV",
        "开始回环自检": "Run loopback self-test",
        "报告通路字节": "Report path byte",
        "手柄内置扬声器（HID + SBC：蓝牙 或 官方USB无线适配器）":
            "Controller speaker (HID + SBC: Bluetooth or official USB wireless adapter)",
        "Windows 音频端点（USB数据线/蓝牙 的 3.5mm 耳机口）":
            "Windows audio endpoint (3.5 mm jack over USB cable / Bluetooth)",
        "0x02（内置扬声器，默认）": "0x02 (internal speaker, default)",
        "0x24（3.5mm 耳机孔）": "0x24 (3.5 mm jack)",
        "预灌缓冲(条)": "Pre-buffer (reports)",
        "送流速率": "Stream rate",
        "控制刷新(条)": "Control refresh (reports)",
        "（仅蓝牙通路：声音断续时，把预灌缓冲加到 12~20，或送流速率改 1.02）":
            "(speaker path only: if audio stutters, raise pre-buffer to 12-20 or set stream rate to 1.02)",
        "1. 输出通路与设备": "1. Output path & device",
        "语言 / Language": "Language",
        "语言已保存，重启后生效": "Language saved. Restart to apply.",
        "立即重启": "Restart now",
        "版本": "Version",
        "DS4 / DualSense 手柄扬声器测试工具": "DS4 / DualSense Controller Speaker Test",
        "音频文件": "Audio files",
        "所有文件": "All files",
        "2. 测试音频（可自定义）": "2. Test audio (customizable)",
        "3. 播放设置": "3. Playback settings",
        "4. 实时电平表": "4. Level meters",
        "5. 回环自检（播放测试音频 → 用麦克风录回来 → 自动判定扬声器是否正常）":
            "5. Loopback self-test (play test audio -> record with a microphone -> automatic verdict)",
        "日志": "Log",
        "内置测试信号": "Built-in test signal",
        "自定义音频文件": "Custom audio file",
        "循环播放": "Loop playback",
        "只显示手柄端点": "Controller endpoints only",
        "WASAPI 独占模式（仅音频端点）": "WASAPI exclusive mode (audio endpoint only)",
        "自动（电脑麦克风）": "Auto (PC microphone)",

        # ---------- 命令行：设备/通路 ----------
        "列出所有音频设备后退出": "List all audio devices and exit",
        "打开图形界面（默认行为）": "Open the GUI (default)",
        "运行离线自检（默认不发声）": "Run offline self-tests (silent by default)",
        "自检时额外做一次静音播放预检": "Also run a silent playback pre-check during self-test",
        "回环自检：播放测试音频并用（手柄）麦克风录音，自动判定扬声器是否正常":
            "Loopback self-test: play test audio and record with a microphone, then judge automatically",
        "走蓝牙 HID/SBC 通路，直接驱动手柄**内置扬声器**（需蓝牙连接）":
            "Drive the controller's **internal speaker** over the HID/SBC path (Bluetooth or official adapter)",
        "列出手柄 HID 接口及其连接方式（蓝牙/USB）":
            "List controller HID interfaces and how they are connected",
        "蓝牙通路离线自检（不向手柄发送数据）": "Offline self-test of the speaker path (sends nothing)",
        "音频报告的输出通路字节：0x02=内置扬声器（默认），0x24=3.5mm 耳机孔":
            "Output path byte of the audio report: 0x02 = internal speaker (default), 0x24 = 3.5 mm jack",
        "SBC 比特率；0（默认）= 跟随参考实现用 ffmpeg 默认参数":
            "SBC bitrate; 0 (default) = use ffmpeg defaults like the reference implementation",
        "每条报告的 SBC 帧数（默认 4 = 0x17/462 字节，不足时自动用 0x14/270）":
            "SBC frames per report (default 4 = 0x17/462 bytes; falls back to 0x14/270)",
        "以独占方式打开手柄 HID": "Open the controller HID interface exclusively",
        "开播前先连发几条报告把手柄缓冲垫起来，抗抖动（默认 6，0=关闭）":
            "Send a few reports up-front to build a jitter buffer (default 6, 0 = off)",
        "送流速率倍数（默认 1.0=严格实时；仍断续可试 1.02 慢慢垫厚缓冲）":
            "Stream rate multiplier (1.0 = realtime; try 1.02 if audio still stutters)",
        "每 N 条音频报告插一条 0x11 刷新控制状态（默认 20，0=关闭）":
            "Insert a 0x11 control report every N audio reports (default 20, 0 = off)",
        "只生成报告不写设备（用于检查参数）": "Build reports without writing to the device",
        "用手柄灯条+震动验证 HID 通路是否打通（不依赖听）":
            "Verify the HID link via lightbar + rumble (no listening required)",
        "控制测试用的灯条颜色 R,G,B（默认 0,255,0）": "Lightbar colour R,G,B (default 0,255,0)",
        "控制测试用的马达强度 0-255（默认 200）": "Rumble strength 0-255 (default 200)",
        "手柄内置扬声器音量 0-255（默认 255 = 100%%）":
            "Controller speaker volume 0-255 (default 255 = 100%%)",
        "有线（USB 数据线）模式：诊断能否驱动扬声器 + 灯条/震动自检 + 自动测 3.5mm 耳机口":
            "Wired mode: diagnose speaker support + lightbar/rumble check + 3.5 mm jack test",
        "有线模式的灯条/震动自检（USB 0x05 报告）":
            "Wired lightbar/rumble check (USB 0x05 report)",
        "--wired 时跳过灯条/震动自检": "Skip the lightbar/rumble check in --wired",
        "输出设备：序号或名称子串，默认自动识别手柄扬声器":
            "Output device: index or name substring; defaults to the controller endpoint",
        "回环测试的录音设备（默认电脑麦克风；插在手柄上的耳麦也可选）":
            "Recording device for the loopback test (default: PC microphone)",
        "自定义测试音频文件（wav/mp3/flac/ogg/m4a...）":
            "Custom test audio file (wav/mp3/flac/ogg/m4a...)",
        "自定义音频的起始位置（秒）": "Start offset of the custom audio (seconds)",
        "播放时长（秒，0=完整播放）": "Playback duration in seconds (0 = full)",
        "单音频率（Hz）": "Tone frequency (Hz)",
        "扫频起始频率（Hz）": "Sweep start frequency (Hz)",
        "扫频终止频率（Hz）": "Sweep end frequency (Hz)",
        "声道路由（测试单侧耳机 / 手柄内置喇叭）": "Channel routing (single earcup / speaker)",
        "音量增益（dB，默认 0）": "Volume gain in dB (default 0)",
        "循环播放": "Loop playback",
        "播放采样率（0=设备默认）": "Playback sample rate (0 = device default)",
        "播放声道数（0=自动）": "Playback channels (0 = auto)",
        "使用 WASAPI 独占模式": "Use WASAPI exclusive mode",
        "音频块大小（0=自动）": "Audio block size (0 = auto)",
        "期望延迟（秒）": "Requested latency (seconds)",
        "把回环测试录到的音频保存为 WAV": "Save the loopback recording as WAV",
        "界面语言：zh=简体中文，en=English（默认跟随系统）":
            "UI language: zh = Chinese, en = English (default: follow the system)",
        "显示版本号并退出": "Show version and exit",
        "输出设备错误：%s": "Output device error: %s",
        "找不到任何音频输出设备": "No audio output device found",
        "找不到任何音频输入设备": "No audio input device found",
        "录音设备错误：%s": "Input device error: %s",
        "输出设备：%s %s": "Output device: %s %s",
        "录音设备：%s %s": "Recording device: %s %s",
        "（已自动识别为手柄扬声器）": "(auto-detected controller output)",
        "（未识别到手柄端点，使用默认输出）": "(no controller endpoint found, using default output)",
        "（手柄耳机口耳麦 —— DS4 没有内置麦克风，需插带麦耳机）":
            "(3.5 mm headset mic - the DS4 has no built-in microphone)",
        "（用电脑麦克风对着手柄录音即可）": "(just point a PC microphone at the controller)",
        "音频：%s": "Audio: %s",
        "开始回环测试：先录音 %.2f 秒 → 播放到手柄扬声器 → 再录 %.2f 秒":
            "Loopback test: record %.2f s -> play to the controller speaker -> record %.2f s",
        "播放结束：共送出 %d 帧，输出峰值 %s dBFS，削波 %d":
            "Playback finished: %d frames sent, output peak %s dBFS, clipped %d",
        "推流结束：%d 条报告 / %d 帧 / %.2f 秒音频（硬件报告 %d 字节，迟到 %d 次，最长 %.1f ms）":
            "Streaming finished: %d reports / %d frames / %.2f s of audio "
            "(report size %d bytes, %d late, max %.1f ms)",
        "已中断。": "Interrupted.",
        "如果你没听到声音：确认通路字节（--bt-path 0x02=内置扬声器 / 0x24=耳机口）、"
        "手柄音量（--bt-volume），以及没有其它程序占用 HID。":
            "If you hear nothing: check the path byte (--bt-path 0x02 = speaker / 0x24 = jack), "
            "the controller volume (--bt-volume), and that no other app owns the HID interface.",

        # ---------- 判定 ----------
        "无法判定": "Undetermined",
        "没有录到任何数据": "No data was captured",
        "录音设备未返回数据": "The recording device returned no data",
        "麦克风几乎录不到声音（扬声器没出声 / 音量太低 / 端点或麦克风选错）%s":
            "The microphone picked up almost nothing (silent speaker / too quiet / wrong endpoint)%s",
        "播放窗口内没有比底噪更响的声音，扬声器基本没出声%s":
            "Nothing louder than the noise floor during playback - the speaker stayed silent%s",
        "录到严重削波，音量过大或扬声器已失真":
            "Severe clipping - volume too high or the speaker is distorting",
        "扬声器正常出声，且与测试音频高度一致%s":
            "The speaker works and matches the test audio closely%s",
        "能听到声音，但与测试音频匹配度偏低（音量偏小/失真/底噪大）%s":
            "Sound is audible but matches poorly (too quiet / distorted / noisy)%s",
        "录到了声音，但不像是在播放测试音频（可能只录到环境噪声）%s":
            "Sound was recorded, but it does not look like the test audio (probably just room noise)%s",
        "麦克风拾音电平（播放窗口内）：峰值 %.1f dBFS / RMS %.1f dBFS":
            "Microphone level (playback window): peak %.1f dBFS / RMS %.1f dBFS",
        "推流前后底噪：%.1f dBFS（信噪比 %.1f dB）":
            "Noise floor before/after streaming: %.1f dBFS (SNR %.1f dB)",
        "频谱相似度 %.2f，包络相关 %s，估计延迟 %s":
            "Spectral similarity %.2f, envelope correlation %s, estimated delay %s",
        "判定：%s —— %s": "Verdict: %s - %s",
        "不适用": "n/a",

        # ---------- 有线模式 ----------
        "================ 有线（USB）模式诊断 ================":
            "================ Wired (USB) mode diagnosis ================",
        "★ 当前接口【可以】驱动内置扬声器：%s":
            "* This interface CAN drive the internal speaker: %s",
        "  直接跑：python ds4_speaker_test.py --bt --signal sweep":
            "  Run: python ds4_speaker_test.py --bt --signal sweep",
        "内置扬声器：当前连接下【不可用】（原因见上）。":
            "Internal speaker: NOT available with the current connection (see above).",
        "下面做两件当前连接下确实能做的事：":
            "The following two checks do work with the current connection:",
        "  1) 灯条/震动自检（USB 0x05 报告）—— 验证 HID 通道":
            "  1) Lightbar/rumble check (USB 0x05 report) - verifies the HID link",
        "  2) 3.5mm 耳机口音频输出测试（请先插上耳机）":
            "  2) 3.5 mm jack audio test (plug in headphones first)",
        "发送 0x05 报告：灯条将变绿并震动一下……":
            "Sending a 0x05 report: the lightbar should turn green and the controller should rumble...",
        "已发送。若灯条没变色/没震动，说明手柄被其它程序独占或数据线接触不良。":
            "Sent. If nothing happened, another app may own the HID interface or the cable is loose.",
        "没找到手柄的音频输出端点（3.5mm 耳机口）：":
            "No controller audio output endpoint (3.5 mm jack) found:",
        "找到手柄音频端点（3.5mm 耳机口）：": "Controller audio endpoints (3.5 mm jack):",
        "→ 请把耳机插到手柄 3.5mm 口上，测试音将送到：%s":
            "-> Plug headphones into the controller's 3.5 mm jack; test audio goes to: %s",
        "手柄接口：%s": "Controller interface: %s",
        "通过 USB 0x05 报告发送：灯条 → RGB%s，马达 → %d/255":
            "Sending over the USB 0x05 report: lightbar -> RGB%s, rumble -> %d/255",
        "请确认：① 灯条变成 RGB%s ② 手柄震动过。":
            "Please confirm: (1) lightbar turned RGB%s (2) the controller rumbled.",

        # ---------- HID / 设备列表 ----------
        "没有找到任何 PlayStation 手柄 HID 接口（请连接手柄后重试）。":
            "No PlayStation controller HID interface found (connect a controller and retry).",
        "找到 %d 个手柄 HID 接口：": "Found %d controller HID interface(s):",
        "USB 数据线直连": "USB cable",
        "蓝牙直连": "Bluetooth",
        "USB 无线适配器": "USB wireless adapter",
        "未知连接": "Unknown link",
        "内置扬声器可用": "speaker available",
        "无音频报告（扬声器不可用）": "no audio reports (speaker unavailable)",
        "已找到：%s": "Found: %s",
        "无法枚举手柄 HID：%s": "Cannot enumerate controller HID: %s",
        "没有找到 PlayStation 手柄 HID 接口": "No PlayStation controller HID interface found",
        "手柄：%s —— 没有音频报告，内置扬声器不可用；插耳机可测 3.5mm 耳机口":
            "Controller: %s - no audio reports, the internal speaker is unavailable; "
            "plug in headphones to test the 3.5 mm jack",
        "检测到 USB 数据线连接：已自动切到「Windows 音频端点」通路。":
            "USB cable detected: switched to the \"Windows audio endpoint\" path automatically.",
        "内置扬声器在数据线连接下物理上无法驱动；想测扬声器请用蓝牙直连或索尼官方 USB 无线适配器 CUH-ZWA1。":
            "The internal speaker cannot be driven over a cable; use Bluetooth or the official "
            "Sony USB wireless adapter (CUH-ZWA1) to test it.",
        "当前通路可测 3.5mm 耳机口（插耳机）+ 灯条/震动自检。":
            "This path can test the 3.5 mm jack (plug in headphones) and run the lightbar/rumble check.",
        "没有找到可用于自检的手柄 HID 接口。": "No controller HID interface available for the check.",
        "灯条/震动自检失败：%s": "Lightbar/rumble check failed: %s",
        "已通过 %s 通路发送 0x11 报告：灯条应变绿、手柄震一下。":
            "Sent a 0x11 report over %s: the lightbar should turn green and the controller should rumble.",
        "已通过 USB 0x05 报告发送：灯条应变绿、手柄震一下（该通路没有音频数据）。":
            "Sent over the USB 0x05 report: the lightbar should turn green and the controller should rumble "
            "(this path carries no audio).",
        "没有找到蓝牙连接的手柄 —— 内置扬声器只能通过蓝牙驱动。\n请拔掉 USB 线，长按 Share + PS 键配对后再试。":
            "No speaker-capable controller found. The internal speaker needs Bluetooth or the official "
            "USB wireless adapter.\nUnplug the USB cable and hold Share + PS to pair, then retry.",
        "▶ 开始播放": "▶ Play",
        "■ 停止": "■ Stop",
        "♪ 完整测试序列": "♪ Full test sequence",
        "☀ 灯条/震动自检": "☀ Lightbar / rumble check",
        "支持 wav / mp3 / flac / ogg / m4a 等（非 WAV 由内置 ffmpeg 解码）": "Supports wav / mp3 / flac / ogg / m4a ... (non-WAV files are decoded by the bundled ffmpeg)",
        "选择测试音频": "Select test audio",
        "请先选择一个有效的音频文件": "Please choose a valid audio file first",
        "自定义文件 %s（%.1f 秒）": "Custom file %s (%.1f s)",
        "内置信号 %s（%.1f 秒）": "Built-in signal %s (%.1f s)",
        "完整测试序列（%.0f 秒）": "Full test sequence (%.0f s)",
        "音频长度为 0，请检查起始 / 时长设置": "Audio length is 0 - check the start/duration settings",
        "准备音频失败：%s": "Failed to prepare audio: %s",
        "单音（默认 1 kHz，可调频率）": "Tone (1 kHz by default, adjustable)",
        "对数扫频 20 Hz → 20 kHz（听杂音/破音）": "Log sweep 20 Hz -> 20 kHz (listen for rattling/clipping)",
        "语音频段扫频 200 Hz → 6 kHz（音量与清晰度）": "Speech-band sweep 200 Hz -> 6 kHz (loudness & clarity)",
        "白噪声（全频段压力测试）": "White noise (full-band stress test)",
        "粉噪声（贴近音乐能量分布）": "Pink noise (music-like energy distribution)",
        "逐声道点名：左 → 右 → 全部": "Channel callout: left -> right -> both",
        "同相 / 反相测试（低频相位）": "In-phase / out-of-phase test (low-frequency polarity)",
        "双音多频 1-0（频响快速校验）": "DTMF 1-0 (quick frequency-response check)",
        "静音（链路预检 / 底噪）": "Silence (link pre-check / noise floor)",
        "完整测试序列（自动依次播放全部项目）": "Full test sequence (plays every item in turn)",
        "单音 %.0f Hz": "Tone %.0f Hz",
        "对数扫频 %.0f Hz → %.0f Hz": "Log sweep %.0f Hz -> %.0f Hz",
        "语音频段扫频 200 Hz → 6000 Hz": "Speech-band sweep 200 Hz -> 6000 Hz",
        "白噪声": "White noise",
        "同相 / 反相测试": "In-phase / out-of-phase test",
        "双音多频序列": "DTMF sequence",
        "静音": "Silence",
        "完整测试序列（约 %.0f 秒）": "Full test sequence (~%.0f s)",
        "削波计数：0": "Clipped: 0",
        "尚未测试。自检会把上面的测试音频播放 1 遍，同时用所选麦克风录音并对比，": "Not tested yet. The self-test plays the audio above once, records with the selected",
        "据此判断手柄扬声器是否真的出声。": "microphone and compares the two to judge whether the speaker really works.",
        "注意：DS4（DualShock 4）没有内置麦克风，Windows 里的 “麦克风 (…Wireless Controller)” ": "Note: the DS4 (DualShock 4) has no built-in microphone. Windows' \"Microphone (...Wireless Controller)\"",
        "是 3.5mm 耳机口上的耳麦麦克风（要插带麦耳机才有信号）。": "is the headset mic on the 3.5 mm jack (it only works with a headset plugged in).",
        "所以请用电脑麦克风对着手柄录音，或选择插在手柄上的耳麦。": "So record with a PC microphone pointed at the controller, or pick the controller headset mic.",
        "正在执行回环自检，请稍候。": "Running the loopback self-test, please wait...",
        "保存回环录音": "Save loopback recording",
        "WAV 文件": "WAV file",
        "录音已保存：%s（%d Hz，%.2f 秒）": "Recording saved: %s (%d Hz, %.2f s)",
        "输出通路：%s": "Output path: %s",
        "手柄内置扬声器（HID + SBC）": "controller speaker (HID + SBC)",
        "Windows 音频端点（耳机口）": "Windows audio endpoint (3.5 mm jack)",
        "查询音频设备失败：%s": "Failed to query audio devices: %s",
        "已刷新设备：输出 %d 个，输入 %d 个；已自动选择 %s": "Devices refreshed: %d outputs, %d inputs; auto-selected %s",
        "无": "none",
        "已识别为手柄音频端点": "recognised as the controller audio endpoint",
        "未识别到手柄端点，声音会送到该设备": "no controller endpoint recognised; audio goes to this device",
        "非 WASAPI 端点（低延迟建议选 WASAPI）": "not a WASAPI endpoint (WASAPI is recommended for low latency)",
        "没有识别到手柄音频端点。\n请确认手柄已用 USB 或蓝牙连接，然后点“刷新设备”。": "No controller audio endpoint found.\nMake sure the controller is connected over USB or Bluetooth, then click “Refresh devices”.",
        "找不到可用的录音设备。": "No usable recording device found.",
        "提示：所选录音设备是手柄耳机口的耳麦，需要插带麦耳机才有信号。": "Note: the selected input is the controller's headset mic - plug in a headset to get a signal.",
        "请先选择输出设备。": "Please select an output device first.",
        "播放失败：%s": "Playback failed: %s",
        "播放内容：%s": "Playing: %s",
        "播放中…": "Playing...",
        "播放到 %s —— %s，音量 %+.1f dB，峰值 %.1f dBFS": "Playing to %s - %s, volume %+.1f dB, peak %.1f dBFS",
        "准备失败：%s": "Preparation failed: %s",
        "未知错误": "unknown error",
        "回环自检失败：%s": "Loopback self-test failed: %s",
        "蓝牙推流：%s；SBC %s；输出=%s": "Streaming: %s; SBC %s; output=%s",
        "内置扬声器": "internal speaker",
        "3.5mm 耳机口": "3.5 mm jack",
        "蓝牙推流失败：%s": "Streaming failed: %s",
        "蓝牙推流失败：%s\n\n": "Streaming failed: %s\n\n",
        "提示：确认手柄是蓝牙连接；若 DS4Windows 正独占该设备请先退出它。": "Note: make sure the controller is on Bluetooth; exit DS4Windows first if it owns the device.",
        "蓝牙推流结束：%d 条报告 / %d 帧 / %.2f 秒音频（报告 %d 字节，迟到 %d 次）": "Streaming finished: %d reports / %d frames / %.2f s of audio (%d-byte reports, %d late)",
        "提示：有 %d 条报告迟到，若听到卡顿可改用 2 帧报告模式。": "Note: %d reports were late; if you hear stuttering, try the 2-frame report mode.",
        "蓝牙推流 %d/%d 帧  %.1f 秒": "Streaming %d/%d frames  %.1f s",
        "回环自检开始：输出 %s → 输入 %s": "Loopback self-test: output %s -> input %s",
        "蓝牙回环自检：手柄输出 → 输入 %s": "Loopback self-test (speaker path): controller -> input %s",
        "蓝牙内置扬声器回环 · %s": "Internal speaker loopback - %s",
        "手柄输出": "controller output",
        "判定：【%s】%s\n采集电平（播放窗口内）：峰值 %.1f dBFS / RMS %.1f dBFS ｜ ": "Verdict: [%s] %s\nCaptured level (playback window): peak %.1f dBFS / RMS %.1f dBFS | ",
        "频谱相似度 %.2f ｜ 包络相关 %s ｜ 估计延迟 %s\n%s": "Spectral similarity %.2f | envelope correlation %s | estimated delay %s\n%s",
        "%.3f 秒": "%.3f s",
        "回环自检结果：%s —— %s（%s）": "Loopback result: %s - %s (%s)",
        "排查建议：把麦克风靠近手柄（10~30 cm）；换另一个 'Wireless Controller' ": "Troubleshooting: move the microphone closer to the controller (10-30 cm); try another 'Wireless Controller' ",
        "输出端点（USB / 蓝牙 A2DP / 蓝牙 HFP 是不同端点）；": "output endpoint (USB / Bluetooth A2DP / Bluetooth HFP are different endpoints);",
        "把音量调到 0 dB 以上，并确认 Windows 音量合成器里该端点音量不为 0。": "raise the volume above 0 dB and make sure the endpoint is not muted in the Windows volume mixer.",
        "输出": "Output",
        "输入": "Input",
        " · 系统默认": " · system default",
        "可用的音频输出设备（%d 个）：": "Available audio output devices (%d):",
        "可用的音频输入设备（%d 个）：": "Available audio input devices (%d):",
        "   ← 手柄扬声器 / 耳机口": "   <- controller speaker / jack",
        "   ← 手柄耳机口耳麦（DS4 无内置麦克风，需插带麦耳机）": "   <- controller headset mic (the DS4 has no built-in mic; a headset is required)",
        "设备序号 %d 不是可用的%s设备": "Device index %d is not a usable %s device",
        "没有名称包含 %r 的%s设备": "No %s device whose name contains %r",
        "自动(0x17/0x14)": "auto (0x17/0x14)",
        "0x%02X/%d 字节": "0x%02X/%d bytes",
        "每 %d 条": "every %d reports",
        "关闭": "off",
        "报告 %s / 写入 %d 字节 / 第3字节 0x%02X / 通路 0x%02X / 音量 %d / ": "reports %s / write %d bytes / byte3 0x%02X / path 0x%02X / volume %d / ",
        "灯条 %s / 预灌 %d 条 / 速率 x%.2f / 控制刷新 %s": "lightbar %s / pre-buffer %d / rate x%.2f / control refresh %s",
        "%d Hz / %d 子带 / %d 块 / %s / %s / 比特池 %d / 帧长 %d 字节": "%d Hz / %d subbands / %d blocks / %s / %s / bitpool %d / frame %d bytes",
        "采样率 %d Hz · 声道 %d · %s": "%d Hz · %d ch · %s",
        "输出报告 %s · %s": "reports %s - %s",
        "%s · %s · 输出报告 %s · %s": "%s - %s - reports %s - %s",
        "   ← 可驱动内置扬声器": "   <- can drive the internal speaker",
        "   ← 不能驱动内置扬声器": "   <- cannot drive the internal speaker",
        "说明：USB 数据线直连时，手柄 HID 描述符里最大输出报告只有 32 字节、":
            "Note: over a USB cable the HID descriptor's largest output report is only 32 bytes and",
        "      且只暴露 0x05（灯条/震动），装不下 270/462 字节的音频报告，":
            "      only report 0x05 (lightbar/rumble) exists - it cannot carry the 270/462-byte audio report,",
        "      所以内置扬声器在数据线连接下物理上无法驱动（实测写入返回 err=87）。":
            "      so the internal speaker physically cannot be driven over a cable (write returns err=87).",
        "      要测内置扬声器：① 蓝牙直连；② 索尼官方 USB 无线适配器 CUH-ZWA1。":
            "      To test the internal speaker: (1) Bluetooth, or (2) the official Sony USB adapter CUH-ZWA1.",
        "      只有数据线时可用 --wired 测 3.5mm 耳机口（插耳机）+ 灯条/震动自检。":
            "      With a cable only, use --wired for the 3.5 mm jack (plug in headphones) + lightbar/rumble check.",
        "这个手柄 HID 接口没有暴露音频报告（0x14/0x17），无法驱动内置扬声器。":
            "This controller HID interface exposes no audio reports (0x14/0x17); "
            "the internal speaker cannot be driven.",
        "  当前连接：%s": "  Current link: %s",
        "  接口输出报告：%s（USB 数据线直连时最大只有 32 字节，装不下 270/462 字节的音频报告）":
            "  Interface output reports: %s (over a USB cable the maximum is 32 bytes - "
            "too small for a 270/462-byte audio report)",
        "能驱动内置扬声器的两种连接方式：":
            "The two connections that can drive the internal speaker:",
        "  1) 蓝牙直连 —— 拔掉 USB 线，长按手柄 Share + PS 键配对电脑；":
            "  1) Bluetooth - unplug the USB cable and hold Share + PS to pair with the PC;",
        "  2) 索尼官方 USB 无线适配器（CUH-ZWA1）—— 手柄与适配器配对后插在 USB 口上。":
            "  2) The official Sony USB wireless adapter (CUH-ZWA1) with the controller paired to it.",
        "只有 USB 数据线时，可以测 3.5mm 耳机口：python ds4_speaker_test.py --wired":
            "With a USB cable only, you can still test the 3.5 mm jack: python ds4_speaker_test.py --wired",
        "说明：DS4 的**内置扬声器**不在 Windows 音频设备列表里，要走 HID + SBC 音频报告通路。":
            "Note: the DS4's **internal speaker** is not a Windows audio device; "
            "it is driven over the HID + SBC audio-report path.",
        "      能不能走，只看手柄 HID 接口有没有暴露音频报告（0x14/0x17）：":
            "      Availability depends only on whether the HID interface exposes the audio reports:",
        "        · 蓝牙直连                                  → 可以":
            "        * Bluetooth                                 -> yes",
        "        · 索尼官方 USB 无线适配器 CUH-ZWA1（插 USB） → 可以（这也算“有线”）":
            "        * Official Sony USB adapter CUH-ZWA1        -> yes (this also counts as \"wired\")",
        "        · USB 数据线直连                            → 不可以（描述符最大输出报告仅 32 字节）":
            "        * USB cable to the controller               -> no (largest output report is 32 bytes)",
        "      只有数据线时用 --wired：测 3.5mm 耳机口 + 灯条/震动自检。":
            "      With a cable only, use --wired: 3.5 mm jack test + lightbar/rumble check.",
        "提示：手柄可能同时提供多个端点（USB / 蓝牙 A2DP / 蓝牙 HFP），":
            "Note: the controller may expose several endpoints (USB / Bluetooth A2DP / Bluetooth HFP);",
        "      若某个端点没有声音，换另一个 'Wireless Controller' 端点再试。":
            "      if one endpoint stays silent, try another 'Wireless Controller' endpoint.",
        "注意：这些音频端点只对应手柄的 **3.5mm 耳机口**；":
            "Note: these audio endpoints map to the controller's **3.5 mm jack** only;",
        "      内置扬声器要走 HID + SBC 通路（--bt），且必须蓝牙直连或官方 USB 无线适配器，":
            "      the internal speaker needs the HID + SBC path (--bt) over Bluetooth or the official adapter,",
        "      只插 USB 数据线时物理上无法驱动（用 --wired 可查看诊断）。":
            "      and cannot be driven over a plain USB cable (run --wired for the diagnosis).",
        "内置扬声器：当前连接下【不可用】（原因见上）。":
            "Internal speaker: NOT available with this connection (see the reason above).",
    },
}
