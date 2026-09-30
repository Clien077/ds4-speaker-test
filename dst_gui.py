# -*- coding: utf-8 -*-
"""DS4 / DualSense 手柄扬声器测试工具的 tkinter 图形界面。"""

from __future__ import annotations

import os
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Dict, List, Optional

import numpy as np

from dst_audio import (
    AudioError, SIGNAL_INFO, analyze_loopback, audio_info, build_signal, load_audio,
    map_channels, resample, route_channels, slice_audio, to_db, write_wav,
)
from dst_devices import AudioDevice, list_devices, pick_input, pick_output
from dst_ds4_bt import (
    OUTPUT_DEFAULT, SBC_FRAMES_PER_SECOND, Ds4BtSpeaker, decode_sbc_frames,
    encode_sbc_file, encode_sbc_pcm, find_ds4_hid_devices, pick_bt_device,
)
from dst_player import Player, Recorder

APP_TITLE = "DS4 / DualSense 手柄扬声器测试工具"
FILE_TYPES = [
    ("音频文件", "*.wav *.mp3 *.flac *.ogg *.oga *.opus *.m4a *.aac *.wma *.aif *.aiff"),
    ("所有文件", "*.*"),
]
SIGNAL_LABELS: List[str] = [
    "%s —— %s" % (key, SIGNAL_INFO[key][0]) for key in SIGNAL_INFO
]
SIGNAL_KEYS: List[str] = list(SIGNAL_INFO.keys())


class SpeakerTestApp:
    """主窗口。"""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("980x760")
        self.root.minsize(880, 680)

        self.out_devices: List[AudioDevice] = []
        self.in_devices: List[AudioDevice] = []
        self.player: Optional[Player] = None
        self.recorder: Optional[Recorder] = None
        self.last_recording: Optional[np.ndarray] = None
        self.last_rec_samplerate: int = 0
        self.last_result: Dict[str, object] = {}
        self.busy = False
        self._meter_hold = np.zeros(2, dtype=np.float64)
        self._meter_hold_db = np.full(2, -99.0)
        # 蓝牙 HID/SBC 通路
        self.bt_devices: list = []
        self.bt_stop = threading.Event()
        self.bt_streaming = False
        self.bt_progress = (0, 0)

        self._build_ui()
        self.refresh_devices()
        self._update_path_mode()
        self.root.after(80, self._poll_meter)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 4}
        root = self.root

        # ---- 1. 输出设备 -------------------------------------------------
        dev_frame = ttk.LabelFrame(root, text="1. 输出通路与设备")
        dev_frame.pack(fill="x", **pad)

        self.path_mode = tk.StringVar(value="bt")
        ttk.Radiobutton(dev_frame, text="蓝牙内置扬声器（HID + SBC，真正的喇叭）",
                        variable=self.path_mode, value="bt",
                        command=self._update_path_mode).grid(row=0, column=0, columnspan=2,
                                                             sticky="w", padx=6, pady=(4, 0))
        ttk.Radiobutton(dev_frame, text="Windows 音频端点（3.5mm 耳机口 / 蓝牙耳机端点）",
                        variable=self.path_mode, value="endpoint",
                        command=self._update_path_mode).grid(row=0, column=2, columnspan=2,
                                                             sticky="w", padx=6, pady=(4, 0))

        ttk.Label(dev_frame, text="报告通路字节").grid(row=1, column=0, sticky="e", padx=6, pady=4)
        self.bt_out_var = tk.StringVar(value="0x02（内置扬声器，默认）")
        self.bt_out_combo = ttk.Combobox(dev_frame, textvariable=self.bt_out_var, state="readonly",
                                         width=26,
                                         values=["0x02（内置扬声器，默认）", "0x24（3.5mm 耳机孔）",
                                                 "0x00", "0x03"])
        self.bt_out_combo.grid(row=1, column=1, sticky="w", pady=4)
        self.bt_info_var = tk.StringVar(value="")
        ttk.Label(dev_frame, textvariable=self.bt_info_var, foreground="#0a5").grid(
            row=1, column=2, columnspan=2, sticky="w", padx=6, pady=4)

        self.dev_var = tk.StringVar()
        self.dev_combo = ttk.Combobox(dev_frame, textvariable=self.dev_var, state="readonly", width=92)
        self.dev_combo.grid(row=2, column=0, columnspan=4, sticky="we", padx=6, pady=4)
        self.dev_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_device_changed())

        ttk.Button(dev_frame, text="刷新设备", command=self.refresh_devices).grid(
            row=3, column=0, sticky="w", padx=6, pady=(0, 6))
        ttk.Button(dev_frame, text="只显示手柄端点", command=self._select_ds4_only).grid(
            row=3, column=1, sticky="w", padx=6, pady=(0, 6))
        self.exclusive_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(dev_frame, text="WASAPI 独占模式（仅音频端点）",
                        variable=self.exclusive_var).grid(row=3, column=2, sticky="w", padx=6, pady=(0, 6))
        self.dev_info_var = tk.StringVar(value="")
        ttk.Label(dev_frame, textvariable=self.dev_info_var, foreground="#0a5").grid(
            row=4, column=0, columnspan=4, sticky="w", padx=8, pady=(0, 6))
        dev_frame.columnconfigure(0, weight=1)

        # ---- 2. 测试音频 -------------------------------------------------
        src_frame = ttk.LabelFrame(root, text="2. 测试音频（可自定义）")
        src_frame.pack(fill="x", **pad)

        self.src_mode = tk.StringVar(value="builtin")
        ttk.Radiobutton(src_frame, text="内置测试信号", variable=self.src_mode, value="builtin",
                        command=self._update_src_state).grid(row=0, column=0, sticky="w", padx=6, pady=3)
        self.sig_var = tk.StringVar(value=SIGNAL_LABELS[0])
        self.sig_combo = ttk.Combobox(src_frame, textvariable=self.sig_var, values=SIGNAL_LABELS,
                                      state="readonly", width=52)
        self.sig_combo.grid(row=0, column=1, columnspan=3, sticky="we", padx=6, pady=3)

        ttk.Label(src_frame, text="频率(Hz)").grid(row=1, column=0, sticky="e", padx=6)
        self.freq_var = tk.StringVar(value="1000")
        ttk.Entry(src_frame, textvariable=self.freq_var, width=10).grid(row=1, column=1, sticky="w")
        ttk.Label(src_frame, text="时长(秒, 0=默认)").grid(row=1, column=2, sticky="e")
        self.dur_var = tk.StringVar(value="0")
        ttk.Entry(src_frame, textvariable=self.dur_var, width=10).grid(row=1, column=3, sticky="w")

        ttk.Radiobutton(src_frame, text="自定义音频文件", variable=self.src_mode, value="file",
                        command=self._update_src_state).grid(row=2, column=0, sticky="w", padx=6, pady=3)
        self.file_var = tk.StringVar(value="")
        self.file_entry = ttk.Entry(src_frame, textvariable=self.file_var, width=60)
        self.file_entry.grid(row=2, column=1, columnspan=2, sticky="we", padx=6, pady=3)
        self.browse_btn = ttk.Button(src_frame, text="浏览...", command=self._browse_file)
        self.browse_btn.grid(row=2, column=3, sticky="w", padx=6)

        ttk.Label(src_frame, text="起始(秒)").grid(row=3, column=0, sticky="e", padx=6)
        self.start_var = tk.StringVar(value="0")
        ttk.Entry(src_frame, textvariable=self.start_var, width=10).grid(row=3, column=1, sticky="w")
        self.file_info_var = tk.StringVar(value="支持 wav / mp3 / flac / ogg / m4a 等（非 WAV 由内置 ffmpeg 解码）")
        ttk.Label(src_frame, textvariable=self.file_info_var, foreground="#666").grid(
            row=4, column=0, columnspan=4, sticky="w", padx=8, pady=(0, 6))
        src_frame.columnconfigure(1, weight=1)

        # ---- 3. 播放设置 -------------------------------------------------
        set_frame = ttk.LabelFrame(root, text="3. 播放设置")
        set_frame.pack(fill="x", **pad)

        ttk.Label(set_frame, text="音量").grid(row=0, column=0, sticky="e", padx=6)
        self.vol_var = tk.DoubleVar(value=0.0)
        self.vol_scale = ttk.Scale(set_frame, from_=-40.0, to=20.0, variable=self.vol_var,
                                   orient="horizontal", length=260, command=lambda _v: self._update_vol_label())
        self.vol_scale.grid(row=0, column=1, sticky="w", padx=4)
        self.vol_label = tk.StringVar(value="0.0 dB")
        ttk.Label(set_frame, textvariable=self.vol_label, width=10).grid(row=0, column=2, sticky="w")

        ttk.Label(set_frame, text="声道路由").grid(row=0, column=3, sticky="e", padx=6)
        self.route_var = tk.StringVar(value="both")
        ttk.Combobox(set_frame, textvariable=self.route_var, state="readonly", width=12,
                     values=["both", "left", "right"]).grid(row=0, column=4, sticky="w")

        self.loop_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(set_frame, text="循环播放", variable=self.loop_var).grid(
            row=0, column=5, sticky="w", padx=12)

        # 蓝牙防卡顿参数（只对蓝牙内置扬声器通路生效）
        ttk.Label(set_frame, text="预灌缓冲(条)").grid(row=1, column=0, sticky="e", padx=6, pady=(4, 0))
        self.lead_var = tk.StringVar(value="6")
        ttk.Entry(set_frame, textvariable=self.lead_var, width=6).grid(
            row=1, column=1, sticky="w", pady=(4, 0))
        ttk.Label(set_frame, text="送流速率").grid(row=1, column=2, sticky="e", pady=(4, 0))
        self.speed_var = tk.StringVar(value="1.00")
        ttk.Entry(set_frame, textvariable=self.speed_var, width=8).grid(
            row=1, column=3, sticky="w", pady=(4, 0))
        ttk.Label(set_frame, text="控制刷新(条)").grid(row=1, column=4, sticky="e", pady=(4, 0))
        self.ctrl_every_var = tk.StringVar(value="20")
        ttk.Entry(set_frame, textvariable=self.ctrl_every_var, width=6).grid(
            row=1, column=5, sticky="w", pady=(4, 0))
        ttk.Label(set_frame, foreground="#666",
                  text="（仅蓝牙通路：声音断续时，把预灌缓冲加到 12~20，或送流速率改 1.02）").grid(
            row=2, column=0, columnspan=6, sticky="w", padx=6, pady=(0, 4))

        # ---- 4. 控制 -----------------------------------------------------
        ctrl_frame = ttk.Frame(root)
        ctrl_frame.pack(fill="x", **pad)
        self.play_btn = ttk.Button(ctrl_frame, text="▶ 开始播放", command=self.start_play)
        self.play_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(ctrl_frame, text="■ 停止", command=self.stop_play)
        self.stop_btn.pack(side="left", padx=4)
        ttk.Button(ctrl_frame, text="♪ 完整测试序列", command=self.play_full_sequence).pack(side="left", padx=4)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(ctrl_frame, textvariable=self.status_var, foreground="#036").pack(side="left", padx=16)

        # ---- 5. 电平表 ---------------------------------------------------
        meter_frame = ttk.LabelFrame(root, text="4. 实时电平表")
        meter_frame.pack(fill="x", **pad)
        self.meter_canvas = tk.Canvas(meter_frame, height=64, background="#111")
        self.meter_canvas.pack(fill="x", padx=8, pady=6)
        self.clip_var = tk.StringVar(value="削波计数：0")
        ttk.Label(meter_frame, textvariable=self.clip_var, foreground="#a00").pack(anchor="w", padx=8, pady=(0, 6))

        # ---- 6. 回环自检 -------------------------------------------------
        lb_frame = ttk.LabelFrame(root, text="5. 回环自检（播放测试音频 → 用麦克风录回来 → 自动判定扬声器是否正常）")
        lb_frame.pack(fill="x", **pad)

        ttk.Label(lb_frame, foreground="#a60", wraplength=930, justify="left",
                  text="注意：DS4（DualShock 4）没有内置麦克风，Windows 里的 “麦克风 (…Wireless Controller)” "
                       "是 3.5mm 耳机口上的耳麦麦克风（要插带麦耳机才有信号）。"
                       "所以请用电脑麦克风对着手柄录音，或选择插在手柄上的耳麦。").grid(
            row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(4, 0))

        self.lb_in_var = tk.StringVar(value="自动（电脑麦克风）")
        self.lb_in_combo = ttk.Combobox(lb_frame, textvariable=self.lb_in_var, state="readonly", width=92)
        self.lb_in_combo.grid(row=1, column=0, columnspan=3, sticky="we", padx=6, pady=4)
        self.lb_btn = ttk.Button(lb_frame, text="开始回环自检", command=self.start_loopback)
        self.lb_btn.grid(row=2, column=0, sticky="w", padx=6, pady=(0, 6))
        self.lb_save_btn = ttk.Button(lb_frame, text="保存录音为 WAV", command=self.save_recording,
                                      state="disabled")
        self.lb_save_btn.grid(row=2, column=1, sticky="w", padx=6, pady=(0, 6))
        self.lb_result_var = tk.StringVar(
            value="尚未测试。自检会把上面的测试音频播放 1 遍，同时用所选麦克风录音并对比，"
                  "据此判断手柄扬声器是否真的出声。")
        ttk.Label(lb_frame, textvariable=self.lb_result_var, wraplength=930, justify="left",
                  foreground="#036").grid(row=3, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 6))
        lb_frame.columnconfigure(0, weight=1)

        # ---- 7. 日志 -----------------------------------------------------
        log_frame = ttk.LabelFrame(root, text="日志")
        log_frame.pack(fill="both", expand=True, **pad)
        self.log = scrolledtext.ScrolledText(log_frame, height=9, wrap="word")
        self.log.pack(fill="both", expand=True, padx=6, pady=6)

        self._update_src_state()

    # ------------------------------------------------------------- 通路切换
    def is_bt_mode(self) -> bool:
        return self.path_mode.get() == "bt"

    def _update_path_mode(self) -> None:
        bt = self.is_bt_mode()
        state = "disabled" if bt else "readonly"
        self.dev_combo.configure(state=state)
        self.exclusive_var.set(False)
        if bt:
            self.refresh_bt_device()
        else:
            self.bt_info_var.set("")
            self._on_device_changed()
        self.log_line("输出通路：%s" % ("蓝牙内置扬声器（HID + SBC）" if bt else "Windows 音频端点"))

    def refresh_bt_device(self) -> None:
        try:
            self.bt_devices = find_ds4_hid_devices()
        except Exception as exc:  # noqa: BLE001
            self.bt_devices = []
            self.bt_info_var.set("无法枚举手柄 HID：%s" % exc)
            return
        dev = pick_bt_device(self.bt_devices, require_bt=True)
        if dev is None:
            if self.bt_devices:
                self.bt_info_var.set("手柄是 USB 连接 —— 内置扬声器必须用蓝牙（拔掉 USB 线重新配对）")
            else:
                self.bt_info_var.set("没有找到 PlayStation 手柄 HID 接口")
        else:
            self.bt_info_var.set("已找到：%s" % dev.label)

    def bt_output_mode(self) -> int:
        text = self.bt_out_var.get().strip()
        try:
            return int(text.split()[0].split("（")[0], 0) & 0xFF
        except (ValueError, IndexError):
            return OUTPUT_DEFAULT

    def _build_bt_frames(self):
        """按界面设置生成 SBC 帧，返回 (frames, params, 说明)。"""
        try:
            start = float(self.start_var.get() or 0)
        except ValueError:
            start = 0.0
        try:
            dur = float(self.dur_var.get() or 0)
        except ValueError:
            dur = 0.0
        gain = float(self.vol_var.get())
        channel = self.route_var.get()
        if self.src_mode.get() == "file":
            path = os.path.expanduser(self.file_var.get().strip().strip('"'))
            if not path or not os.path.isfile(path):
                raise AudioError("请先选择一个有效的音频文件")
            frames, params = encode_sbc_file(path, gain_db=gain, channel=channel,
                                             start=start, duration=dur)
            desc = "自定义文件 %s（%.1f 秒）" % (os.path.basename(path),
                                            len(frames) / SBC_FRAMES_PER_SECOND)
        else:
            try:
                freq = float(self.freq_var.get() or 1000)
            except ValueError:
                freq = 1000.0
            data, text = build_signal(self._selected_signal_key(), samplerate=32000, channels=2,
                                      duration=dur or None, freq=freq, amplitude=0.9)
            frames, params = encode_sbc_pcm(data, gain_db=gain, channel=channel)
            desc = "内置信号 %s（%.1f 秒）" % (text, len(frames) / SBC_FRAMES_PER_SECOND)
        return frames, params, desc

    def _bt_tuning(self):
        """读取界面上的防卡顿参数。"""
        def num(var, default, cast=float):
            try:
                return cast(str(var.get()).strip())
            except (ValueError, AttributeError):
                return default

        return (max(0, num(self.lead_var, 6, int)),
                num(self.speed_var, 1.0, float) or 1.0,
                max(0, num(self.ctrl_every_var, 20, int)))

    def _new_bt_speaker(self):
        dev = pick_bt_device(self.bt_devices, require_bt=True)
        if dev is None:
            raise RuntimeError("没有蓝牙连接的手柄 —— 内置扬声器只能通过蓝牙驱动。\n"
                               "请拔掉 USB 线，长按 Share + PS 键配对后再试。")
        lead, speed, ctrl_every = self._bt_tuning()
        return Ds4BtSpeaker(dev, output_mode=self.bt_output_mode(),
                            frames_per_report=4, lead_reports=lead, speed=speed,
                            control_every=ctrl_every, log=self.log_line)

    # ------------------------------------------------------------- 设备管理
    def refresh_devices(self) -> None:
        try:
            self.out_devices = [d for d in list_devices("output") if d.max_output_channels > 0]
            self.in_devices = [d for d in list_devices("input") if d.max_input_channels > 0]
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_TITLE, "查询音频设备失败：%s" % exc)
            return

        self.dev_combo["values"] = [d.label for d in self.out_devices]
        self.lb_in_combo["values"] = ["自动（电脑麦克风）"] + [d.label for d in self.in_devices]

        best = pick_output(self.out_devices)
        if best is not None:
            for i, d in enumerate(self.out_devices):
                if d.index == best.index:
                    self.dev_combo.current(i)
                    break
        self._on_device_changed()
        self.refresh_bt_device()
        self.log_line("已刷新设备：输出 %d 个，输入 %d 个；已自动选择 %s"
                      % (len(self.out_devices), len(self.in_devices), best.name if best else "无"))

    def _select_ds4_only(self) -> None:
        ds4 = [d for d in self.out_devices if d.is_ds4]
        if not ds4:
            messagebox.showinfo(APP_TITLE, "没有识别到手柄音频端点。\n请确认手柄已用 USB 或蓝牙连接，然后点“刷新设备”。")
            return
        self.dev_combo["values"] = [d.label for d in ds4]
        self.dev_combo.current(0)
        self._on_device_changed()

    def current_output(self) -> Optional[AudioDevice]:
        idx = self.dev_combo.current()
        if idx < 0 or idx >= len(self.out_devices):
            return None
        return self.out_devices[idx]

    def current_input(self) -> Optional[AudioDevice]:
        idx = self.lb_in_combo.current()
        if idx <= 0:
            return None
        j = idx - 1
        if 0 <= j < len(self.in_devices):
            return self.in_devices[j]
        return None

    def _on_device_changed(self) -> None:
        dev = self.current_output()
        if dev is None:
            return
        sr = int(round(dev.default_samplerate))
        ch = max(1, min(2, dev.max_output_channels))
        notes = []
        if dev.is_ds4:
            notes.append("已识别为手柄音频端点")
        else:
            notes.append("未识别到手柄端点，声音会送到该设备")
        if "wasapi" not in dev.hostapi_name.lower():
            notes.append("非 WASAPI 端点（低延迟建议选 WASAPI）")
        self.dev_info_var.set("采样率 %d Hz · 声道 %d · %s" % (sr, ch, "；".join(notes)))

    def _browse_file(self) -> None:
        path = filedialog.askopenfilename(title="选择测试音频", filetypes=FILE_TYPES)
        if not path:
            return
        self.file_var.set(path)
        self.src_mode.set("file")
        self._update_src_state()
        info = audio_info(path)
        if info.get("ok"):
            self.file_info_var.set("%s：%.1f 秒 · %d Hz · %d 声道"
                                   % (os.path.basename(path), info["duration"],
                                      info["samplerate"], info["channels"]))
        else:
            self.file_info_var.set("无法读取该文件：%s" % info.get("error", "未知错误"))

    def _update_src_state(self) -> None:
        builtin = self.src_mode.get() == "builtin"
        state_b = "readonly" if builtin else "disabled"
        state_f = "disabled" if builtin else "normal"
        self.sig_combo.configure(state=state_b)
        self.file_entry.configure(state=state_f)
        self.browse_btn.configure(state=state_f)

    def _update_vol_label(self) -> None:
        self.vol_label.set("%+.1f dB" % self.vol_var.get())

    # ---------------------------------------------------------------- 工具
    def log_line(self, text: str) -> None:
        self.log.insert("end", time.strftime("[%H:%M:%S] ") + text + "\n")
        self.log.see("end")

    def _gain(self) -> float:
        return 10.0 ** (float(self.vol_var.get()) / 20.0)

    def _selected_signal_key(self) -> str:
        idx = self.sig_combo.current()
        if idx < 0:
            return "tone"
        return SIGNAL_KEYS[idx]

    def build_source(self, samplerate: int, channels: int):
        """按界面设置生成 / 加载待播放音频，返回 (samples, 说明)。"""
        mode = self.src_mode.get()
        if mode == "file":
            path = os.path.expanduser(self.file_var.get().strip().strip('"'))
            if not path or not os.path.isfile(path):
                raise AudioError("请先选择一个有效的音频文件")
            data, sr = load_audio(path, samplerate=samplerate, channels=channels)
            try:
                start = float(self.start_var.get() or 0)
            except ValueError:
                start = 0.0
            try:
                dur = float(self.dur_var.get() or 0)
            except ValueError:
                dur = 0.0
            data = slice_audio(data, sr, start, dur)
            desc = "自定义文件 %s（%.1f 秒）" % (os.path.basename(path), data.shape[0] / sr)
        else:
            key = self._selected_signal_key()
            try:
                freq = float(self.freq_var.get() or 1000)
            except ValueError:
                freq = 1000.0
            try:
                dur = float(self.dur_var.get() or 0)
            except ValueError:
                dur = 0.0
            data, text = build_signal(key, samplerate=samplerate, channels=channels,
                                      duration=dur or None, freq=freq)
            desc = "内置信号 %s（%.1f 秒）" % (text, data.shape[0] / samplerate)
        data = map_channels(data, channels)
        data = route_channels(data, self.route_var.get())
        if data.shape[0] == 0:
            raise AudioError("音频长度为 0，请检查起始 / 时长设置")
        return np.ascontiguousarray(data, dtype=np.float32), desc

    # ---------------------------------------------------------------- 播放
    def start_play(self, signal_override: Optional[str] = None) -> None:
        if self.busy:
            messagebox.showinfo(APP_TITLE, "正在执行回环自检，请稍候。")
            return
        if self.is_bt_mode():
            self._start_bt_play(signal_override)
            return
        dev = self.current_output()
        if dev is None:
            messagebox.showwarning(APP_TITLE, "请先选择输出设备。")
            return
        if self.player is not None and self.player.is_playing:
            self.stop_play()

        samplerate = int(round(dev.default_samplerate))
        channels = max(1, min(2, dev.max_output_channels))
        try:
            if signal_override:
                data, _ = build_signal(signal_override, samplerate=samplerate, channels=channels)
                data = route_channels(map_channels(data, channels), self.route_var.get())
                desc = "完整测试序列（%.0f 秒）" % (data.shape[0] / samplerate)
            else:
                data, desc = self.build_source(samplerate, channels)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_TITLE, "准备音频失败：%s" % exc)
            return

        gain = self._gain()
        self.player = Player(
            device=dev.index, samplerate=samplerate, channels=channels,
            volume=gain, loop=bool(self.loop_var.get()),
            exclusive=bool(self.exclusive_var.get()),
        )
        try:
            self.player.start(data)
        except RuntimeError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            self.log_line("播放失败：%s" % exc)
            return

        self.log_line("播放到 %s —— %s，音量 %+.1f dB，峰值 %.1f dBFS"
                      % (dev.name, desc, self.vol_var.get(),
                         20 * np.log10(max(float(np.max(np.abs(data))) * gain, 1e-9))))
        self.status_var.set("播放中…")
        self.clip_var.set("削波计数：0")
        self._meter_hold_db[:] = -99.0
        self.player.meter.reset()

    def play_full_sequence(self) -> None:
        self.start_play(signal_override="all")

    def stop_play(self) -> None:
        if self.bt_streaming:
            self.bt_stop.set()
            self.status_var.set("正在停止蓝牙推流…")
            return
        if self.player is not None:
            self.player.stop()
        self.status_var.set("已停止")

    # -------------------------------------------------------- 蓝牙推流（线程）
    def _start_bt_play(self, signal_override: Optional[str]) -> None:
        if self.bt_streaming:
            self.stop_play()
            return
        self.refresh_bt_device()
        try:
            speaker = self._new_bt_speaker()
            if signal_override:
                data, text = build_signal(signal_override, samplerate=32000, channels=2)
                frames, params = encode_sbc_pcm(data, gain_db=float(self.vol_var.get()),
                                                channel=self.route_var.get())
                desc = "完整测试序列（%.0f 秒）" % (len(frames) / SBC_FRAMES_PER_SECOND)
            else:
                frames, params, desc = self._build_bt_frames()
        except (RuntimeError, AudioError) as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return

        self.bt_stop.clear()
        self.bt_streaming = True
        self.bt_progress = (0, len(frames))
        self.status_var.set("蓝牙推流中…")
        self.log_line("蓝牙推流：%s；SBC %s；输出=%s" % (
            desc, params.describe(),
            "内置扬声器" if self.bt_output_mode() == "speaker" else "3.5mm 耳机口"))
        threading.Thread(target=self._bt_play_worker,
                         args=(speaker, frames, desc), daemon=True).start()

    def _bt_play_worker(self, speaker, frames, desc: str) -> None:
        result = None
        error: Optional[str] = None
        try:
            speaker.open()
            while not self.bt_stop.is_set():
                speaker.play(frames, realtime=True, stop_flag=self.bt_stop,
                             progress=lambda sent, total: setattr(self, "bt_progress", (sent, total)))
                if not self.loop_var.get():
                    break
            result = speaker.result
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
        finally:
            speaker.close()
        self.root.after(0, lambda: self._bt_play_finished(result, error))

    def _bt_play_finished(self, result, error: Optional[str]) -> None:
        self.bt_streaming = False
        if error:
            self.status_var.set("蓝牙推流出错")
            self.log_line("蓝牙推流失败：%s" % error)
            messagebox.showerror(APP_TITLE, "蓝牙推流失败：%s\n\n"
                                 "提示：确认手柄是蓝牙连接；若 DS4Windows 正独占该设备请先退出它。" % error)
            return
        if result is None:
            self.status_var.set("已停止")
            return
        self.status_var.set("蓝牙推流结束：%d 帧 / %.1f 秒" % (result.frames_sent, result.audio_seconds))
        self.log_line("蓝牙推流结束：%d 条报告 / %d 帧 / %.2f 秒音频（报告 %d 字节，迟到 %d 次）"
                      % (result.reports_sent, result.frames_sent, result.audio_seconds,
                         result.report_size_used, result.late_reports))
        if result.late_reports > 5:
            self.log_line("提示：有 %d 条报告迟到，若听到卡顿可改用 2 帧报告模式。" % result.late_reports)

    # -------------------------------------------------------------- 电平表
    def _poll_meter(self) -> None:
        try:
            if self.bt_streaming:
                sent, total = self.bt_progress
                self._draw_bt_progress(sent, total)
                self.status_var.set("蓝牙推流中… %d/%d 帧（%.1f/%.1f 秒）"
                                    % (sent, total, sent / SBC_FRAMES_PER_SECOND,
                                       max(total, 1) / SBC_FRAMES_PER_SECOND))
            elif self.player is not None:
                snap = self.player.meter.snapshot()
                peak_db = list(snap["peak_db"])
                clipped = int(snap["clipped"])
                if clipped:
                    total = int(self.clip_var.get().split("：")[-1]) + clipped
                    self.clip_var.set("削波计数：%d" % total)
                for i in range(min(2, len(peak_db))):
                    self._meter_hold_db[i] = max(peak_db[i], self._meter_hold_db[i] - 1.5)
                self._draw_meter()
                if not self.player.is_playing and self.status_var.get() == "播放中…":
                    self.status_var.set("播放结束（共 %d 帧）" % self.player.played_frames)
        finally:
            self.root.after(80, self._poll_meter)

    def _draw_bt_progress(self, sent: int, total: int) -> None:
        c = self.meter_canvas
        c.delete("all")
        w = max(c.winfo_width(), 200)
        h = max(c.winfo_height(), 60)
        frac = (sent / total) if total else 0.0
        c.create_rectangle(6, 10, w - 6, h - 10, outline="#444", fill="#181818")
        if frac > 0:
            c.create_rectangle(6, 10, 6 + frac * (w - 12), h - 10, outline="", fill="#3498db")
        c.create_text(w // 2, h // 2, fill="#fff",
                      text="蓝牙推流 %d/%d 帧  %.1f 秒" % (sent, total, sent / SBC_FRAMES_PER_SECOND))

    def _draw_meter(self) -> None:
        c = self.meter_canvas
        c.delete("all")
        w = max(c.winfo_width(), 200)
        h = max(c.winfo_height(), 60)
        bar_h = h // 2 - 8
        for i in range(2):
            y0 = 6 + i * (bar_h + 8)
            y1 = y0 + bar_h
            c.create_rectangle(6, y0, w - 70, y1, outline="#444", fill="#181818")
            frac = (max(-60.0, min(0.0, self._meter_hold_db[i])) + 60.0) / 60.0 if i < len(self._meter_hold_db) else 0.0
            x1 = 6 + frac * (w - 76)
            color = "#2ecc71" if frac < 0.75 else ("#f1c40f" if frac < 0.95 else "#e74c3c")
            if x1 > 7:
                c.create_rectangle(6, y0, x1, y1, outline="", fill=color)
            c.create_text(w - 60, (y0 + y1) / 2, anchor="w", fill="#ddd",
                          text="ch%d %6.1f dB" % (i + 1, self._meter_hold_db[i]))

    # ------------------------------------------------------------ 回环自检
    def start_loopback(self) -> None:
        if self.busy:
            return
        if self.is_bt_mode():
            self.refresh_bt_device()
            in_dev = self.current_input() or pick_input(self.in_devices)
            if in_dev is None:
                messagebox.showwarning(APP_TITLE, "找不到可用的录音设备。")
                return
            if in_dev.is_ds4:
                self.log_line("提示：所选录音设备是手柄耳机口的耳麦，需要插带麦耳机才有信号。")
            if self.bt_streaming:
                self.stop_play()
            self.busy = True
            self.lb_btn.configure(state="disabled")
            self.play_btn.configure(state="disabled")
            self.lb_result_var.set("正在自检：录音 → 蓝牙推流到手柄 → 录音 → 分析，请等待…")
            self.log_line("蓝牙回环自检：手柄输出 → 输入 %s" % in_dev.name)
            threading.Thread(target=self._bt_loopback_worker, args=(in_dev,), daemon=True).start()
            return

        out_dev = self.current_output()
        if out_dev is None:
            messagebox.showwarning(APP_TITLE, "请先选择输出设备。")
            return
        in_dev = self.current_input() or pick_input(self.in_devices, peer=out_dev)
        if in_dev is None:
            messagebox.showwarning(APP_TITLE, "找不到可用的录音设备。")
            return
        if in_dev.is_ds4:
            self.log_line("提示：所选录音设备是手柄耳机口的耳麦，需要插带麦耳机才有信号。")
        if self.player is not None and self.player.is_playing:
            self.stop_play()

        self.busy = True
        self.lb_btn.configure(state="disabled")
        self.play_btn.configure(state="disabled")
        self.lb_result_var.set("正在自检：录音 → 播放 → 录音 → 分析，请等待…")
        self.log_line("回环自检开始：输出 %s → 输入 %s" % (out_dev.name, in_dev.name))
        threading.Thread(target=self._loopback_worker, args=(out_dev, in_dev), daemon=True).start()

    def _loopback_worker(self, out_dev: AudioDevice, in_dev: AudioDevice) -> None:
        out_sr = int(round(out_dev.default_samplerate))
        in_sr = int(round(in_dev.default_samplerate))
        out_ch = max(1, min(2, out_dev.max_output_channels))
        in_ch = max(1, min(2, in_dev.max_input_channels))
        try:
            data, desc = self.build_source(out_sr, out_ch)
        except Exception as exc:  # noqa: BLE001
            self._finish_loopback(None, None, 0, "准备音频失败：%s" % exc, out_dev, in_dev)
            return

        gain = self._gain()
        player = Player(device=out_dev.index, samplerate=out_sr, channels=out_ch,
                        volume=gain, exclusive=bool(self.exclusive_var.get()))
        recorder = Recorder(device=in_dev.index, samplerate=in_sr, channels=in_ch)
        preroll, tail = 0.35, 0.8
        play_seconds = data.shape[0] / float(out_sr)
        try:
            recorder.start()
            time.sleep(preroll)
            player.start(data)
            player.wait(timeout=play_seconds + 6.0)
            time.sleep(tail)
        except RuntimeError as exc:
            recorder.stop()
            player.stop()
            self._finish_loopback(None, None, 0, str(exc), out_dev, in_dev)
            return
        rec = recorder.stop()
        player.stop()

        if rec.shape[0] == 0:
            self._finish_loopback(None, None, 0, "没有录到任何数据", out_dev, in_dev)
            return
        analysis_sr = max(out_sr, in_sr)
        ref = resample(data, out_sr, analysis_sr) if out_sr != analysis_sr else data
        rec_r = resample(rec, in_sr, analysis_sr) if in_sr != analysis_sr else rec
        result = analyze_loopback(ref, rec_r, analysis_sr, preroll_s=preroll)
        self._finish_loopback(result, rec, in_sr,
                              "播放内容：%s" % desc, out_dev, in_dev)

    def _bt_loopback_worker(self, in_dev: AudioDevice) -> None:
        in_sr = int(round(in_dev.default_samplerate))
        in_ch = max(1, min(2, in_dev.max_input_channels))
        try:
            speaker = self._new_bt_speaker()
            frames, params, desc = self._build_bt_frames()
        except Exception as exc:  # noqa: BLE001
            self._finish_loopback(None, None, 0, "准备失败：%s" % exc, None, in_dev)
            return

        ref = decode_sbc_frames(frames)
        recorder = Recorder(device=in_dev.index, samplerate=in_sr, channels=in_ch)
        preroll, tail = 0.5, 1.0
        try:
            speaker.open()
            recorder.start()
            time.sleep(preroll)
            speaker.play(frames, realtime=True)
            time.sleep(tail)
        except Exception as exc:  # noqa: BLE001
            recorder.stop()
            speaker.close()
            self._finish_loopback(None, None, 0, str(exc), None, in_dev)
            return
        rec = recorder.stop()
        speaker.close()

        if rec.shape[0] == 0:
            self._finish_loopback(None, None, 0, "没有录到任何数据", None, in_dev)
            return
        analysis_sr = max(32000, in_sr)
        ref_r = ref if 32000 == analysis_sr else resample(ref, 32000, analysis_sr)
        rec_r = rec if in_sr == analysis_sr else resample(rec, in_sr, analysis_sr)
        result = analyze_loopback(ref_r, rec_r, analysis_sr, preroll_s=preroll)
        self._finish_loopback(result, rec, in_sr, "蓝牙内置扬声器回环 · %s" % desc, None, in_dev)

    def _finish_loopback(self, result, rec, in_sr: int, note: str,
                         out_dev: AudioDevice, in_dev: AudioDevice) -> None:
        def apply() -> None:
            self.busy = False
            self.lb_btn.configure(state="normal")
            self.play_btn.configure(state="normal")
            if result is None:
                self.lb_result_var.set("自检失败：%s" % note)
                self.log_line("回环自检失败：%s" % note)
                return
            self.last_result = dict(result)
            if rec is not None:
                self.last_recording = rec
                self.last_rec_samplerate = int(in_sr)
                self.lb_save_btn.configure(state="normal")
            text = ("判定：【%s】%s\n采集电平（播放窗口内）：峰值 %.1f dBFS / RMS %.1f dBFS ｜ "
                    "频谱相似度 %.2f ｜ 包络相关 %s ｜ 估计延迟 %s\n%s"
                    % (str(result["verdict"]).upper(), result["verdict_text"],
                       result["rec_peak_db_main"], result["rec_rms_db_main"], result["spec_sim"],
                       "%.2f" % result["env_corr"] if result["env_corr"] is not None else "不适用",
                       "%.3f 秒" % result["delay_s"] if result["delay_s"] is not None else "不适用",
                       note))
            self.lb_result_var.set(text)
            self.log_line("回环自检结果：%s —— %s（%s）"
                          % (result["verdict"], result["verdict_text"], result["detail"]))
            if result["verdict"] in ("silent", "clipped", "unclear"):
                self.log_line("排查建议：把麦克风靠近手柄（10~30 cm）；换另一个 'Wireless Controller' "
                              "输出端点（USB / 蓝牙 A2DP / 蓝牙 HFP 是不同端点）；"
                              "把音量调到 0 dB 以上，并确认 Windows 音量合成器里该端点音量不为 0。")

        self.root.after(0, apply)

    def save_recording(self) -> None:
        if self.last_recording is None:
            return
        path = filedialog.asksaveasfilename(
            title="保存回环录音", defaultextension=".wav",
            initialfile="ds4_speaker_loopback.wav", filetypes=[("WAV 文件", "*.wav")])
        if not path:
            return
        from dst_audio import write_wav

        write_wav(path, self.last_recording, self.last_rec_samplerate, "int16")
        self.log_line("录音已保存：%s（%d Hz，%.2f 秒）"
                      % (path, self.last_rec_samplerate,
                         self.last_recording.shape[0] / float(self.last_rec_samplerate or 1)))

    # ---------------------------------------------------------------- 关闭
    def _on_close(self) -> None:
        try:
            self.bt_stop.set()
            if self.player is not None:
                self.player.stop()
            if self.recorder is not None:
                self.recorder.stop()
        except Exception:  # noqa: BLE001
            pass
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.25)
    except Exception:  # noqa: BLE001
        pass
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:  # noqa: BLE001
        pass
    SpeakerTestApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
