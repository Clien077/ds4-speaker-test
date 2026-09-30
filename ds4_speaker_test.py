# -*- coding: utf-8 -*-
"""DS4 / DualSense 手柄扬声器测试工具（命令行入口）。

用法示例：
    python ds4_speaker_test.py                       # 打开图形界面
    python ds4_speaker_test.py --list                # 列出所有音频设备
    python ds4_speaker_test.py --signal sweep        # 播放内置对数扫频
    python ds4_speaker_test.py --file 我的音乐.mp3   # 播放自定义测试音频
    python ds4_speaker_test.py --loopback-test       # 自动回环自检（扬声器→手柄麦克风）
    python ds4_speaker_test.py --selftest            # 离线自检（不发声）
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import tempfile
import time
from typing import Dict, List, Optional, Tuple

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dst_audio import (  # noqa: E402
    AudioError, SIGNAL_INFO, analyze_loopback, build_signal, load_audio, map_channels,
    parse_wav, read_wav, resample, route_channels, signal_stats, slice_audio, tone, to_db,
    write_wav,
)
from dst_devices import (  # noqa: E402
    AudioDevice, format_device_list, list_devices, match_device,
)
from dst_player import Player, Recorder  # noqa: E402
from dst_ds4_bt import (  # noqa: E402
    AUDIO_BYTE2, AUDIO_REPORT_ID_2, AUDIO_REPORT_ID_4, AUDIO_REPORT_SIZE_2,
    AUDIO_REPORT_SIZE_4, OUTPUT_DEFAULT, SBC_DEFAULT_BITRATE, SBC_FRAME_LENGTH_PS4,
    SBC_FRAMES_PER_SECOND, Ds4BtSpeaker, bitpool_for_frame_length, bitrate_for_bitpool,
    build_control_report, build_raw_audio_report, crc32_bt, crc32_bt_reference,
    decode_sbc_frames, encode_sbc_file, encode_sbc_pcm, find_ds4_hid_devices,
    format_hid_list, parse_sbc_params, pick_bt_device, split_sbc_frames,
)

EXIT_OK = 0
EXIT_WARN = 1
EXIT_FAIL = 2

DEFAULT_AMPLITUDE = 0.25  # -12 dBFS（走 Windows 音频端点时用）
#: 走蓝牙内置喇叭时默认给满一点：DS4 的小喇叭本身音量很小（-1 dBFS）
BT_DEFAULT_AMPLITUDE = 0.9


def _attach_parent_console() -> None:
    """打包成 GUI 子系统 exe 后没有控制台：从 cmd/PowerShell 启动时借用父进程的控制台。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        if not ctypes.windll.kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
            return
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
    except Exception:  # noqa: BLE001
        pass


def _setup_console() -> None:
    """保证 stdout/stderr 一定可用：打包后双击运行时丢弃输出，命令行调用时输出到父控制台。"""
    if sys.stdout is None or sys.stderr is None:
        _attach_parent_console()
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass


def _db_bar(db: float, width: int = 24) -> str:
    """把 dBFS 画成进度条。"""
    frac = (max(-60.0, min(0.0, db)) + 60.0) / 60.0
    filled = int(round(frac * width))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


# --------------------------------------------------------------------------- #
# 参数解析辅助
# --------------------------------------------------------------------------- #

def resolve_output(spec: Optional[str], verbose: bool = True) -> AudioDevice:
    try:
        dev = match_device(spec, "output")
    except ValueError as exc:
        raise SystemExit("输出设备错误：%s\n可用设备：\n%s" % (exc, format_device_list("output")))
    if dev is None:
        raise SystemExit("找不到任何音频输出设备")
    if verbose:
        tag = "（已自动识别为手柄扬声器）" if dev.is_ds4 else "（未识别到手柄端点，使用默认输出）"
        print("输出设备：%s %s" % (dev.label, tag))
    return dev


def resolve_input(spec: Optional[str], peer: Optional[AudioDevice], verbose: bool = True) -> AudioDevice:
    try:
        if spec:
            dev = match_device(spec, "input")
        else:
            from dst_devices import pick_input

            dev = pick_input(list_devices("input"), peer=peer)
    except ValueError as exc:
        raise SystemExit("输入设备错误：%s\n可用设备：\n%s" % (exc, format_device_list("input")))
    if dev is None:
        raise SystemExit("找不到任何音频输入设备")
    if verbose:
        tag = ("（手柄耳机口耳麦 —— DS4 没有内置麦克风，需插带麦耳机）" if dev.is_ds4
               else "（用电脑麦克风对着手柄录音即可）")
        print("录音设备：%s %s" % (dev.label, tag))
    return dev


def device_samplerate(dev: AudioDevice, override: Optional[int]) -> int:
    if override:
        return int(override)
    return int(round(dev.default_samplerate))


def device_channels(dev: AudioDevice, want: Optional[int] = None) -> int:
    if want:
        return max(1, min(int(want), max(dev.channels, 1)))
    return max(1, min(2, dev.channels))


def build_source(args, samplerate: int, channels: int) -> Tuple[np.ndarray, str]:
    """根据命令行参数取得待播放音频（自定义文件优先）。"""
    if args.file:
        path = os.path.expanduser(args.file)
        try:
            data, sr = load_audio(path, samplerate=samplerate, channels=channels)
        except AudioError as exc:
            raise SystemExit("加载音频文件失败：%s" % exc)
        data = slice_audio(data, sr, args.start, args.duration)
        desc = "%s（%.1f 秒）" % (os.path.basename(path), data.shape[0] / sr)
    else:
        try:
            data, desc = build_signal(
                args.signal, samplerate=samplerate, channels=channels,
                duration=args.duration or None, amplitude=DEFAULT_AMPLITUDE,
                freq=args.freq, f0=args.f0, f1=args.f1,
            )
        except AudioError as exc:
            raise SystemExit(str(exc))
        desc = "内置信号：%s（%.1f 秒）" % (desc, data.shape[0] / samplerate)
    data = map_channels(data, channels)
    data = route_channels(data, args.channel)
    if data.shape[0] == 0:
        raise SystemExit("要播放的音频长度为 0，请检查 --start / --duration 参数")
    return np.ascontiguousarray(data, dtype=np.float32), desc


def print_stats(prefix: str, samples: np.ndarray, samplerate: int) -> None:
    st = signal_stats(samples)
    print(
        "%s：%d 帧 / %d 声道 / %.2f 秒，峰值 %.1f dBFS，RMS %.1f dBFS%s"
        % (
            prefix, st["frames"], st["channels"], st["frames"] / float(samplerate or 1),
            st["peak_db"], st["rms_db"],
            "，削波 %d 个采样" % st["clipped"] if st["clipped"] else "",
        )
    )


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #

def cmd_list(_args) -> int:
    print(format_device_list("all"))
    print("")
    print("提示：手柄可能同时提供多个端点（USB / 蓝牙 A2DP / 蓝牙 HFP），")
    print("      若某个端点没有声音，换另一个 'Wireless Controller' 端点再试。")
    return EXIT_OK


def cmd_play(args) -> int:
    out_dev = resolve_output(args.device)
    samplerate = device_samplerate(out_dev, args.samplerate)
    channels = device_channels(out_dev, args.channels)
    data, desc = build_source(args, samplerate, channels)
    gain = 10.0 ** (args.volume_db / 20.0)

    print("音频：%s" % desc)
    print_stats("送出信号", data * gain, samplerate)
    if args.exclusive:
        print("模式：WASAPI 独占")

    player = Player(
        device=out_dev.index, samplerate=samplerate, channels=channels,
        volume=gain, loop=args.loop, exclusive=args.exclusive,
        blocksize=args.blocksize, latency=args.latency,
    )
    try:
        player.start(data)
    except RuntimeError as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return EXIT_FAIL

    is_tty = sys.stdout.isatty()
    session_peak = [0.0] * channels
    session_rms_max = [0.0] * channels
    session_clipped = 0
    try:
        while player.is_playing:
            time.sleep(0.1)
            snap = player.meter.snapshot()
            for i in range(min(channels, len(snap["peak"]))):
                session_peak[i] = max(session_peak[i], float(snap["peak"][i]))
                session_rms_max[i] = max(session_rms_max[i], float(snap["rms"][i]))
            session_clipped += int(snap["clipped"])
            if is_tty:
                peaks = " ".join("ch%d %6.1f dBFS" % (i + 1, v)
                                 for i, v in enumerate(snap["peak_db"][:2]))
                line = "\r播放中 %s %s 削波 %d" % (
                    _db_bar(max(snap["peak_db"] or [-99.0])), peaks, session_clipped)
                sys.stdout.write(line[:160].ljust(140))
                sys.stdout.flush()
            if not args.loop and player.duration and player.played_frames >= len(data):
                break
    except KeyboardInterrupt:
        print("\n已中断。")
    finally:
        if is_tty:
            sys.stdout.write("\r" + " " * 140 + "\r")
        player.stop()

    snap = player.meter.snapshot()
    for i in range(min(channels, len(snap["peak"]))):
        session_peak[i] = max(session_peak[i], float(snap["peak"][i]))
        session_rms_max[i] = max(session_rms_max[i], float(snap["rms"][i]))
    session_clipped += int(snap["clipped"])

    print("播放结束：共送出 %d 帧，输出峰值 %s dBFS，削波 %d"
          % (player.played_frames, ["%.1f" % to_db(v) for v in session_peak], session_clipped))
    if session_clipped:
        print("警告：输出出现削波，请调低音量（--volume-db）。")
    return EXIT_OK


def cmd_loopback(args) -> int:
    out_dev = resolve_output(args.device)
    in_dev = resolve_input(args.input_device, out_dev)
    out_sr = device_samplerate(out_dev, args.samplerate)
    in_sr = int(round(in_dev.default_samplerate))
    out_ch = device_channels(out_dev, args.channels)
    in_ch = max(1, min(2, in_dev.max_input_channels))

    data, desc = build_source(args, out_sr, out_ch)
    gain = 10.0 ** (args.volume_db / 20.0)
    print("音频：%s" % desc)
    print("扬声器采样率 %d Hz / %d ch，麦克风采样率 %d Hz / %d ch" % (out_sr, out_ch, in_sr, in_ch))

    player = Player(device=out_dev.index, samplerate=out_sr, channels=out_ch,
                    volume=gain, loop=False, exclusive=args.exclusive, blocksize=args.blocksize)
    recorder = Recorder(device=in_dev.index, samplerate=in_sr, channels=in_ch,
                        blocksize=args.blocksize)

    preroll, tail = 0.30, 0.80
    play_seconds = data.shape[0] / float(out_sr)
    print("开始回环测试：先录音 %.2f 秒 → 播放到手柄扬声器 → 再录 %.2f 秒" % (preroll, tail))
    try:
        recorder.start()
        time.sleep(preroll)
        player.start(data)
        player.wait(timeout=play_seconds + 5.0)
        time.sleep(tail)
    except RuntimeError as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return EXIT_FAIL
    finally:
        rec = recorder.stop()
        player.stop()

    print("录制完成：%.2f 秒（%d 帧）" % (rec.shape[0] / float(in_sr or 1), rec.shape[0]))
    if rec.shape[0] == 0:
        print("错误：没有录到数据", file=sys.stderr)
        return EXIT_FAIL

    analysis_sr = max(out_sr, in_sr)
    ref = resample(data, out_sr, analysis_sr) if out_sr != analysis_sr else data
    rec_r = resample(rec, in_sr, analysis_sr) if in_sr != analysis_sr else rec
    result = analyze_loopback(ref, rec_r, analysis_sr, preroll_s=preroll)

    print("")
    print("================ 回环测试结果 ================")
    print("麦克风拾音电平（播放窗口内）：峰值 %.1f dBFS / RMS %.1f dBFS"
          % (result["rec_peak_db_main"], result["rec_rms_db_main"]))
    print("整段录音：峰值 %.1f dBFS / RMS %.1f dBFS"
          % (result["rec_peak_db"], result["rec_rms_db"]))
    print("频谱相似度 %.2f，包络相关 %s，估计延迟 %s"
          % (result["spec_sim"],
             "%.2f" % result["env_corr"] if result["env_corr"] is not None else "不适用",
             "%.3f 秒" % result["delay_s"] if result["delay_s"] is not None else "不适用"))
    if result["ref_dom_hz"] and result["rec_dom_hz"]:
        print("主频：参考 %.0f Hz / 录回 %.0f Hz" % (result["ref_dom_hz"], result["rec_dom_hz"]))
    print("判定：%s —— %s" % (result["verdict"].upper(), result["verdict_text"]))
    print("==============================================")

    if str(result["verdict"]) in ("silent", "clipped", "unclear"):
        print("")
        print("排查建议：")
        print("  1) 把电脑麦克风靠近手柄（10~30 cm），或在手柄上插一副带麦耳机后用 --input-device 选它；")
        print("  2) 换另一个 'Wireless Controller' 输出端点（USB / 蓝牙 A2DP / 蓝牙 HFP 是不同的端点）；")
        print("  3) 调高音量：--volume-db 6；确认 Windows 音量合成器里该端点的音量不为 0。")
        print("  可用录音设备：")
        for d in list_devices("input")[:8]:
            print("     " + d.label)

    if args.save_rec:
        out_path = os.path.abspath(args.save_rec)
        write_wav(out_path, rec, in_sr, "int16")
        print("录音已保存：%s" % out_path)

    verdict = str(result["verdict"])
    if verdict == "pass":
        return EXIT_OK
    if verdict in ("silent", "clipped"):
        return EXIT_FAIL
    return EXIT_WARN


# --------------------------------------------------------------------------- #
# 蓝牙内置扬声器（HID + SBC）
# --------------------------------------------------------------------------- #

def cmd_bt_list(_args) -> int:
    print(format_hid_list())
    print("")
    print("说明：DS4 的**内置喇叭**不在 Windows 音频设备列表里，必须走蓝牙 HID/SBC 通路；")
    print("      如果上面显示的是 USB，请拔掉 USB 线并用 Share+PS 键把手柄配到电脑蓝牙。")
    return EXIT_OK


def _bt_source(args, bitrate: int):
    """取得 SBC 帧列表（自定义文件优先）。"""
    if args.file:
        path = os.path.expanduser(args.file)
        frames, params = encode_sbc_file(
            path, bitrate=bitrate, gain_db=args.volume_db, channel=args.channel,
            start=args.start, duration=args.duration,
        )
        desc = "%s（%.1f 秒）" % (os.path.basename(path), len(frames) / SBC_FRAMES_PER_SECOND)
    else:
        data, text = build_signal(
            args.signal, samplerate=32000, channels=2,
            duration=args.duration or None, amplitude=BT_DEFAULT_AMPLITUDE,
            freq=args.freq, f0=args.f0, f1=args.f1,
        )
        frames, params = encode_sbc_pcm(
            data, bitrate=bitrate, gain_db=args.volume_db, channel=args.channel,
        )
        desc = "内置信号 %s（%.1f 秒）" % (text, len(frames) / SBC_FRAMES_PER_SECOND)
    return frames, params, desc


def cmd_bt_loopback(args) -> int:
    """蓝牙推流 + 电脑麦克风回采，客观判断内置扬声器是否真的出声。"""
    devices = find_ds4_hid_devices()
    dev = pick_bt_device(devices, require_bt=True)
    if dev is None:
        print(format_hid_list(devices))
        print("错误：需要蓝牙连接的手柄。", file=sys.stderr)
        return EXIT_FAIL
    in_dev = resolve_input(args.input_device, None)
    in_sr = int(round(in_dev.default_samplerate))
    in_ch = max(1, min(2, in_dev.max_input_channels))

    try:
        frames, params, desc = _bt_source(args, args.bt_bitrate)
    except AudioError as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return EXIT_FAIL
    print("手柄接口：%s" % dev.label)
    print("输出通路字节：0x%02X" % args.bt_path)
    print("音频：%s；SBC %s" % (desc, params.describe()))
    print("回采设备：%s（%d Hz / %d ch）" % (in_dev.name, in_sr, in_ch))

    ref = decode_sbc_frames(frames)  # 实际发出去的音频，作为对比基准
    speaker = Ds4BtSpeaker(dev, output_mode=args.bt_path,
                           frames_per_report=args.bt_frames, exclusive=args.bt_exclusive,
                           volume=args.bt_volume, lead_reports=args.bt_lead,
                           speed=args.bt_speed, control_every=args.bt_control_every)
    recorder = Recorder(device=in_dev.index, samplerate=in_sr, channels=in_ch)
    preroll, tail = 0.5, 1.0
    try:
        speaker.open()
        recorder.start()
        time.sleep(preroll)
        print("正在推流（%.1f 秒）……" % (len(frames) / SBC_FRAMES_PER_SECOND))
        speaker.play(frames, realtime=True)
        time.sleep(tail)
    except (OSError, RuntimeError) as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return EXIT_FAIL
    finally:
        rec = recorder.stop()
        speaker.close()

    if rec.shape[0] == 0:
        print("错误：没有录到数据", file=sys.stderr)
        return EXIT_FAIL
    analysis_sr = max(32000, in_sr)
    ref_r = ref if 32000 == analysis_sr else resample(ref, 32000, analysis_sr)
    rec_r = rec if in_sr == analysis_sr else resample(rec, in_sr, analysis_sr)
    result = analyze_loopback(ref_r, rec_r, analysis_sr, preroll_s=preroll)

    print("")
    print("============ 蓝牙内置扬声器回环结果 ============")
    print("麦克风拾音电平（推流窗口内）：峰值 %.1f dBFS / RMS %.1f dBFS"
          % (result["rec_peak_db_main"], result["rec_rms_db_main"]))
    if result.get("floor_db") is not None:
        print("推流前后底噪：%.1f dBFS（信噪比 %.1f dB）"
              % (result["floor_db"], result["snr_db"]))
    print("频谱相似度 %.2f，包络相关 %s，估计延迟 %s"
          % (result["spec_sim"],
             "%.2f" % result["env_corr"] if result["env_corr"] is not None else "不适用",
             "%.3f 秒" % result["delay_s"] if result["delay_s"] is not None else "不适用"))
    print("判定：%s —— %s" % (result["verdict"].upper(), result["verdict_text"]))
    print("===============================================")
    if args.save_rec:
        out_path = os.path.abspath(args.save_rec)
        write_wav(out_path, rec, in_sr, "int16")
        print("录音已保存：%s" % out_path)
    if str(result["verdict"]) in ("silent", "unclear"):
        print("提示：把电脑麦克风靠近手柄（10~30 cm）；或试 --bt-path 0x24 走耳机口；")
        print("      确认手柄音量、以及没有别的程序（如 DS4Windows）占用 HID 接口。")
    verdict = str(result["verdict"])
    if verdict == "pass":
        return EXIT_OK
    if verdict in ("silent", "clipped"):
        return EXIT_FAIL
    return EXIT_WARN


def cmd_bt_control_test(args) -> int:
    """用灯条 + 震动验证 HID 通路（肉眼/手感可确认，不依赖听）。"""
    devices = find_ds4_hid_devices()
    dev = pick_bt_device(devices, require_bt=True)
    if dev is None:
        print(format_hid_list(devices))
        print("错误：需要蓝牙连接的手柄。", file=sys.stderr)
        return EXIT_FAIL
    try:
        color = tuple(int(x) for x in args.bt_color.split(","))
        if len(color) != 3:
            raise ValueError
    except ValueError:
        print("错误：--bt-color 需要形如 0,255,0 的三个数值", file=sys.stderr)
        return EXIT_FAIL
    rumble = max(0, min(255, int(args.bt_rumble)))

    speaker = Ds4BtSpeaker(dev, output_mode=args.bt_path, volume=args.bt_volume, led=color)
    try:
        speaker.open()
    except (OSError, RuntimeError) as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return EXIT_FAIL

    print("手柄接口：%s" % dev.label)
    print("发送 0x11 控制报告：灯条 → RGB%s，马达 → %d/255" % (color, rumble))
    try:
        for _ in range(3):
            speaker.send_control_report((rumble, rumble))
            time.sleep(0.4)
        print("保持 2.5 秒，请观察灯条 / 手感震动……")
        time.sleep(2.5)
        speaker.send_control_report((0, 0))
    except OSError as exc:
        print("写入手柄失败：%s" % exc, file=sys.stderr)
        return EXIT_FAIL
    finally:
        speaker.close()

    print("")
    print("请确认两点：")
    print("  1) 手柄灯条有没有变成 RGB%s？" % (color,))
    print("  2) 手柄有没有震动？")
    print("如果两者都有：HID 通路与 0x11 报告（含 audio_control=0x20）已被手柄接受，")
    print("               此时若 --bt 仍无声，问题在音频报告/SBC 流或 3.5mm 插入检测。")
    return EXIT_OK


def cmd_bt_play(args) -> int:
    devices = find_ds4_hid_devices()
    dev = pick_bt_device(devices, require_bt=True)
    if dev is None:
        print(format_hid_list(devices))
        print("")
        if devices:
            print("错误：找到手柄，但它是 USB 连接。内置扬声器只能通过蓝牙驱动：", file=sys.stderr)
            print("      拔掉 USB 线 → 手柄长按 Share + PS 键进入配对 → 在 Windows 蓝牙里添加“Wireless Controller”。",
                  file=sys.stderr)
        else:
            print("错误：没有找到 PlayStation 手柄的 HID 接口。", file=sys.stderr)
        return EXIT_FAIL

    print("手柄接口：%s" % dev.label)
    print("输出通路字节：0x%02X（实测 0x24 能让内置喇叭出声；可试 0x02/0x00/0x03）" % args.bt_path)

    try:
        frames, params, desc = _bt_source(args, args.bt_bitrate)
    except AudioError as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return EXIT_FAIL
    print("音频：%s" % desc)
    print("SBC：%s" % params.describe())
    print("共 %d 帧，将分 %d 条报告发送（每帧 1/250 秒）"
          % (len(frames), (len(frames) + args.bt_frames - 1) // args.bt_frames))

    speaker = Ds4BtSpeaker(
        dev, output_mode=args.bt_path, frames_per_report=args.bt_frames,
        exclusive=args.bt_exclusive, volume=args.bt_volume,
        lead_reports=args.bt_lead, speed=args.bt_speed,
        control_every=args.bt_control_every,
        log=lambda m: print("  · %s" % m),
    )
    print("报告参数：%s" % speaker.describe())
    try:
        if args.bt_dry_run:
            print("（--bt-dry-run：只生成报告，不写入设备）")
            report = build_raw_audio_report(AUDIO_REPORT_ID_2, AUDIO_REPORT_SIZE_2,
                                            frames[:args.bt_frames], 0,
                                            speaker.output_mode, True, AUDIO_BYTE2)
            print("示例报告 %d 字节，ID=0x%02X，CRC=%s"
                  % (len(report), report[0], report[-4:].hex(" ")))
            return EXIT_OK
        speaker.open()
    except (OSError, RuntimeError) as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return EXIT_FAIL

    print("开始推流……（按 Ctrl+C 可中断）")
    last = [0.0]

    def progress(sent: int, total: int) -> None:
        now = time.time()
        if now - last[0] >= 1.0 or sent >= total:
            last[0] = now
            sys.stdout.write("\r已送出 %5.1f / %.1f 秒" % (sent / SBC_FRAMES_PER_SECOND,
                                                        total / SBC_FRAMES_PER_SECOND))
            sys.stdout.flush()

    rounds = 0
    try:
        while True:
            result = speaker.play(frames, realtime=True, progress=progress)
            rounds += 1
            if not args.loop:
                break
    except KeyboardInterrupt:
        print("\n已中断。")
        return EXIT_OK
    except OSError as exc:
        print("\n写入手柄失败：%s" % exc, file=sys.stderr)
        print("提示：确认手柄是蓝牙连接；若 DS4Windows 正独占该设备，请先退出它。", file=sys.stderr)
        return EXIT_FAIL
    finally:
        speaker.close()

    sys.stdout.write("\r" + " " * 40 + "\r")
    print("推流结束：%d 条报告 / %d 帧 / %.2f 秒音频（硬件报告 %d 字节，迟到 %d 次，最长 %.1f ms）"
          % (result.reports_sent, result.frames_sent, result.audio_seconds,
             result.report_size_used, result.late_reports, result.max_late_ms))
    if rounds > 1:
        print("循环次数：%d" % rounds)
    if result.late_reports > 5:
        print("提示：有 %d 条报告迟到（最长 %.1f ms），若听到卡顿可试 --bt-lead 12 加大预灌缓冲，"
              "或 --bt-speed 1.02。" % (result.late_reports, result.max_late_ms))
    print("如果你没听到声音：确认通路字节（--bt-path 0x02=内置扬声器 / 0x24=耳机口）、"
          "手柄音量（--bt-volume），以及没有其它程序占用 HID。")
    return EXIT_OK


def cmd_bt_selftest(_args) -> int:
    """蓝牙通路的离线自检（不向手柄发送任何数据）。"""
    import random

    failures: List[str] = []
    checks = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal checks
        checks += 1
        print("  [%s] %s%s" % ("通过" if ok else "失败", name, (" —— " + detail) if detail else ""))
        if not ok:
            failures.append(name)

    print("== 1. CRC32（Linux 内核算法：0xA2 + payload 的标准 CRC-32）==")
    import zlib

    same = True
    for n in (0, 1, 5, 74, 1000):
        d = bytes(random.randrange(256) for _ in range(n))
        if crc32_bt(d) != crc32_bt_reference(d):
            same = False
    check("zlib 实现与逐位参考实现一致", same)
    check("空负载等于 0xA2 单字节的 CRC",
          crc32_bt(b"") == zlib.crc32(b"\xa2", 0), hex(crc32_bt(b"")))

    print("== 2. SBC 编码 ==")
    sig = tone(1000.0, 1.0, 32000, 2, 0.5)
    frames, params = encode_sbc_pcm(sig)
    check("1 秒音频正好 250 帧", len(frames) == 250, "实际 %d 帧" % len(frames))
    check("参数为 32 kHz / 8 子带 / 16 块", params.samplerate == 32000
          and params.subbands == 8 and params.blocks == 16, params.describe())
    check("帧长一致", len(set(len(f) for f in frames)) == 1,
          "帧长 %d 字节" % params.frame_length)
    check("4 帧放得进 0x17 报告（462 字节）",
          4 * params.frame_length + 10 <= AUDIO_REPORT_SIZE_4)
    check("每帧都能被解析", all(parse_sbc_params(f).frame_length == len(f)
                                for f in frames[:20]))

    print("== 3. 回解验证 ==")
    back = decode_sbc_frames(frames)
    freqs = np.fft.rfftfreq(back.shape[0], 1.0 / 32000)
    dom = float(freqs[int(np.argmax(np.abs(np.fft.rfft(back[:, 0]))))])
    peak = float(np.max(np.abs(back)))
    check("回解时长 1.000 秒", abs(back.shape[0] / 32000.0 - 1.0) < 0.01,
          "%.3f 秒" % (back.shape[0] / 32000.0))
    check("回解主频 1000 Hz", abs(dom - 1000.0) < 20.0, "%.0f Hz" % dom)
    check("回解电平正常", abs(20 * np.log10(max(peak, 1e-9)) + 6.0) < 1.5,
          "峰值 %.1f dBFS（输入 -6 dBFS）" % (20 * np.log10(max(peak, 1e-9))))

    print("== 4. 自定义音频文件 ==")
    tmpdir = tempfile.mkdtemp(prefix="ds4_bt_")
    wav = os.path.join(tmpdir, "t.wav")
    write_wav(wav, tone(440.0, 0.5, 44100, 1, 0.6), 44100, "int16")
    fframes, fparams = encode_sbc_file(wav, gain_db=-6.0)
    check("44.1 kHz 单声道 wav 编码", abs(len(fframes) / SBC_FRAMES_PER_SECOND - 0.5) < 0.02,
          "%d 帧 / %.3f 秒" % (len(fframes), len(fframes) / SBC_FRAMES_PER_SECOND))
    check("重采样到 32 kHz", fparams.samplerate == 32000, fparams.describe())

    print("== 5. 报告打包 ==")
    ctrl = build_control_report()
    check("控制报告 78 字节 / ID 0x11", len(ctrl) == 78 and ctrl[0] == 0x11)
    check("audio_control=0x20（启用音频路径，不能是 0xA2）", ctrl[2] == 0x20,
          "0x%02X" % ctrl[2])
    check("哈 control 字节 0xC0/0xF3/0x44", ctrl[1] == 0xC0 and ctrl[3] == 0xF3 and ctrl[4] == 0x44)
    check("音量字段 = 43 43 00 pct 85", list(ctrl[21:26]) == [0x43, 0x43, 0x00, 100, 0x85],
          str(list(ctrl[21:26])))
    rep = build_raw_audio_report(AUDIO_REPORT_ID_2, AUDIO_REPORT_SIZE_2, frames[:2], 0,
                                 OUTPUT_DEFAULT, True, AUDIO_BYTE2)
    rep4 = build_raw_audio_report(AUDIO_REPORT_ID_4, AUDIO_REPORT_SIZE_4, frames[:4], 0,
                                  OUTPUT_DEFAULT, True, AUDIO_BYTE2)
    check("音频报告 270 字节 / ID 0x14", len(rep) == 270 and rep[0] == 0x14,
          "实际 %d 字节 / ID 0x%02X" % (len(rep), rep[0]))
    check("音频报告 462 字节 / ID 0x17", len(rep4) == 462 and rep4[0] == 0x17)
    check("第 3 字节为 0xA0", rep[2] == AUDIO_BYTE2 and rep4[2] == AUDIO_BYTE2)
    check("通路字节 0x02 = 内置扬声器", rep[5] == 0x02, "0x%02X" % rep[5])
    check("音频紧跟 6 字节报告头", rep[6:6 + len(frames[0])] == frames[0])
    check("第 2 帧位置正确", rep[6 + params.frame_length:6 + 2 * params.frame_length] == frames[1])
    check("尾部为填充（手柄不校验 CRC）",
          all(b == 0 for b in rep[6 + 2 * params.frame_length:AUDIO_REPORT_SIZE_2 - 4]))
    check("报告尾部 CRC 自洽（0xA2 + 前面全部）",
          rep[-4:] == crc32_bt(rep[:-4]).to_bytes(4, "little"))
    padded = build_raw_audio_report(AUDIO_REPORT_ID_4, AUDIO_REPORT_SIZE_4, frames[:4], 0,
                                    OUTPUT_DEFAULT, True, AUDIO_BYTE2, write_size=547)
    check("按集合最大长度写满 547 字节（Windows 的实际发送长度）",
          len(padded) == 547, "实际 %d 字节" % len(padded))
    check("CRC 仍在真实长度的末尾 [458:462]",
          padded[458:462] == crc32_bt(padded[:458]).to_bytes(4, "little"))
    check("78 字节控制报告同样写满 547",
          len(build_control_report(write_size=547)) == 547
          and build_control_report(write_size=547)[74:78]
          == crc32_bt(build_control_report(write_size=547)[:74]).to_bytes(4, "little"))
    check("帧计数写入正确",
          build_raw_audio_report(AUDIO_REPORT_ID_2, AUDIO_REPORT_SIZE_2, frames[:2], 300,
                                 OUTPUT_DEFAULT, True, AUDIO_BYTE2)[3:5]
          == bytes([300 & 0xFF, 300 >> 8]))

    print("== 6. 手柄 HID 接口 ==")
    try:
        devices = find_ds4_hid_devices()
    except Exception as exc:  # noqa: BLE001
        devices = []
        print("  查询失败：%s" % exc)
    for d in devices:
        print("    %s" % d.label)
        print("      %s" % d.path)
    bt_dev = pick_bt_device(devices, require_bt=True)
    usb_dev = pick_bt_device(devices, require_bt=False)
    if usb_dev is None:
        print("  [跳过] 当前没有连接手柄")
    elif bt_dev is None:
        print("  [注意] 手柄现在是 USB 连接 —— 内置扬声器需要蓝牙（拔掉 USB 线重新配对）")
    else:
        check("找到蓝牙手柄接口", True, bt_dev.label)
        try:
            speaker = Ds4BtSpeaker(bt_dev)
            check("可构造推流器（未实际发送）", True, speaker.describe())
        except Exception as exc:  # noqa: BLE001
            check("可构造推流器（未实际发送）", False, str(exc))

    print("")
    if failures:
        print("蓝牙通路自检：%d 项检查，%d 项失败 -> %s" % (checks, len(failures), "; ".join(failures)))
        return EXIT_FAIL
    print("蓝牙通路自检：%d 项检查全部通过。" % checks)
    return EXIT_OK


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #

def cmd_selftest(args) -> int:
    failures: List[str] = []
    checks = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal checks
        checks += 1
        print("  [%s] %s%s" % ("通过" if ok else "失败", name, (" —— " + detail) if detail else ""))
        if not ok:
            failures.append(name)

    print("== 1. 设备枚举 ==")
    outs = list_devices("output")
    ins = list_devices("input")
    print("  输出设备 %d 个，输入设备 %d 个" % (len(outs), len(ins)))
    ds4_out = [d for d in outs if d.is_ds4]
    ds4_in = [d for d in ins if d.is_ds4]
    for d in ds4_out:
        print("    手柄输出: " + d.label)
    for d in ds4_in:
        print("    手柄输入（耳机口耳麦，需插带麦耳机）: " + d.label)
    check("枚举到音频设备", bool(outs) and bool(ins))
    if ds4_out:
        check("识别到手柄音频端点", True, "%d 个" % len(ds4_out))
    else:
        print("  [注意] 没有手柄音频端点（Windows 音频类）：蓝牙只配了 HID 设备时属正常现象，")
        print("         内置喇叭请用 --bt；要测耳机口可手动指定 --device")

    print("== 2. 内置测试信号 ==")
    for key in SIGNAL_INFO:
        if key == "all":
            continue
        sig, desc = build_signal(key, samplerate=48000, channels=2, duration=0.5)
        ok = sig.size > 0 and np.all(np.isfinite(sig)) and float(np.max(np.abs(sig))) <= 1.0
        check("信号 %s (%s)" % (key, desc), ok,
              "峰值 %.3f，%d 帧" % (float(np.max(np.abs(sig))) if sig.size else 0.0, sig.shape[0]))
    all_sig, marks = build_signal("all", samplerate=48000, channels=2)
    check("完整测试序列", all_sig.shape[0] > 48000 and len(marks) >= 5,
          "%.1f 秒，%d 个项目" % (all_sig.shape[0] / 48000.0, len(marks)))

    print("== 3. WAV 读写往返 ==")
    tmpdir = tempfile.mkdtemp(prefix="ds4_spk_")
    base = tone(1000.0, 0.5, 48000, 2, 0.5)
    p32 = os.path.join(tmpdir, "t32.wav")
    write_wav(p32, base, 48000, "float32")
    back32, sr32 = read_wav(p32)
    check("float32 WAV 往返", sr32 == 48000 and back32.shape == base.shape
          and float(np.max(np.abs(back32 - base))) < 1e-6)
    p16 = os.path.join(tmpdir, "t16.wav")
    write_wav(p16, base, 48000, "int16")
    back16, sr16 = read_wav(p16)
    err16 = float(np.max(np.abs(back16 - base))) if back16.shape == base.shape else 1.0
    check("int16 WAV 往返", sr16 == 48000 and err16 < 2.0 / 32768 * 2, "最大误差 %.2e" % err16)

    print("== 4. 重采样 ==")
    t1 = tone(1000.0, 1.0, 48000, 1, 0.5)
    for target in (32000, 16000, 44100):
        rs = resample(t1, 48000, target)
        dur_ok = abs(rs.shape[0] / float(target) - 1.0) < 0.02
        freqs = np.fft.rfftfreq(rs.shape[0], 1.0 / target)
        dom = float(freqs[int(np.argmax(np.abs(np.fft.rfft(rs[:, 0]))))])
        check("48000 → %d Hz" % target, dur_ok and abs(dom - 1000.0) < 10.0,
              "时长 %.3f 秒，主频 %.0f Hz" % (rs.shape[0] / float(target), dom))
    # 增益必须是 0 dB（曾经因为用 |w| 归一化而整体衰减 3.6 dB）
    for f in (100.0, 1000.0, 4000.0, 12000.0):
        src = tone(f, 0.4, 32000, 1, 0.5)[:, 0]
        dst = resample(src[:, None], 32000, 48000)[:, 0]
        a = float(np.sqrt(np.mean(src[2000:-2000] ** 2)))
        b = float(np.sqrt(np.mean(dst[3000:-3000] ** 2)))
        gain_db = 20.0 * math.log10(max(b, 1e-9) / max(a, 1e-9))
        check("32000 → 48000 Hz @ %.0f Hz 增益" % f, abs(gain_db) < 0.5, "%+.2f dB" % gain_db)

    print("== 5. 声道映射 / 路由 ==")
    mono = tone(1000.0, 0.1, 48000, 1, 0.5)
    check("单声道 → 双声道", map_channels(mono, 2).shape == (mono.shape[0], 2))
    check("双声道 → 单声道", map_channels(tone(1000.0, 0.1, 48000, 2, 0.5), 1).shape[1] == 1)
    lr = route_channels(np.ones((10, 2), dtype=np.float32), "left")
    check("仅左声道输出", bool(np.all(lr[:, 1] == 0) and np.all(lr[:, 0] == 1)))

    print("== 6. 回环判定算法 ==")
    bursts = np.concatenate([
        tone(1000.0, 0.4, 48000, 1, 0.4), np.zeros((int(0.4 * 48000), 1), np.float32),
        tone(1000.0, 0.4, 48000, 1, 0.4), np.zeros((int(0.6 * 48000), 1), np.float32),
        tone(2000.0, 0.4, 48000, 1, 0.4),
    ], axis=0)
    delay = int(0.25 * 48000)
    fake = np.concatenate([np.zeros((delay, 1), np.float32), bursts * 0.12]) \
        + np.random.default_rng(1).standard_normal((bursts.shape[0] + delay, 1)).astype(np.float32) * 2e-4
    res = analyze_loopback(bursts, fake, 48000, preroll_s=0.0)
    ok = res["verdict"] in ("pass", "weak") and res["delay_s"] is not None and abs(res["delay_s"] - 0.25) <= 0.06
    check("延迟/相似度估计", bool(ok), "判定 %s，延迟 %.3f 秒，相似度 %.2f"
          % (res["verdict"], res["delay_s"] if res["delay_s"] is not None else -1, res["spec_sim"]))
    silent = np.random.default_rng(2).standard_normal((48000 * 2, 1)).astype(np.float32) * 1e-5
    res2 = analyze_loopback(bursts, silent, 48000)
    check("静音识别", res2["verdict"] == "silent", "判定 %s" % res2["verdict"])

    print("== 7. ffmpeg 解码 ==")
    from dst_audio import find_ffmpeg

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("  [跳过] 未找到 ffmpeg（自定义非 WAV 音频将不可用）")
    else:
        import subprocess

        src = os.path.join(tmpdir, "src.wav")
        write_wav(src, tone(1000.0, 1.0, 48000, 2, 0.5), 48000, "int16")
        for ext in ("flac", "mp3", "ogg"):
            enc = os.path.join(tmpdir, "enc." + ext)
            proc = subprocess.run(
                [ffmpeg, "-hide_banner", "-v", "error", "-y", "-i", src, enc],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=0x08000000 if sys.platform == "win32" else 0,
            )
            if proc.returncode != 0:
                print("  [跳过] 无法编码 %s（该 ffmpeg 构建缺少对应编码器）" % ext)
                continue
            decoded, sr = load_audio(enc, samplerate=32000, channels=2)
            freqs = np.fft.rfftfreq(decoded.shape[0], 1.0 / sr)
            dom = float(freqs[int(np.argmax(np.abs(np.fft.rfft(decoded[:, 0]))))])
            check("解码 %s 并重采样到 32 kHz" % ext,
                  abs(dom - 1000.0) < 20.0 and abs(decoded.shape[0] / sr - 1.0) < 0.1,
                  "主频 %.0f Hz，时长 %.2f 秒" % (dom, decoded.shape[0] / sr))

    print("== 8. 输出链路预检（播放静音，不会有声音）==")
    if args.with_audio:
        out_dev = resolve_output(args.device, verbose=False)
        sr = device_samplerate(out_dev, args.samplerate)
        ch = device_channels(out_dev, args.channels)
        player = Player(device=out_dev.index, samplerate=sr, channels=ch, volume=0.0)
        try:
            player.start(np.zeros((sr // 2, ch), dtype=np.float32))
            player.wait(timeout=5.0)
            player.stop()
            played = player.played_frames
            check("在 %s 上打开输出流" % out_dev.name, played > 0, "送出 %d 帧静音" % played)
        except RuntimeError as exc:
            check("在 %s 上打开输出流" % out_dev.name, False, str(exc))
    else:
        print("  [跳过] 加 --with-audio 可验证输出设备能否真正打开")

    print("")
    if failures:
        print("自检结果：%d 项检查，%d 项失败 -> %s" % (checks, len(failures), "; ".join(failures)))
        return EXIT_FAIL
    print("自检结果：%d 项检查全部通过。临时文件位于 %s" % (checks, tmpdir))
    return EXIT_OK


def cmd_gui(_args) -> int:
    try:
        from dst_gui import main as gui_main
    except ImportError as exc:
        print("无法加载图形界面（需要 tkinter）：%s" % exc, file=sys.stderr)
        print("可改用命令行模式，例如：python ds4_speaker_test.py --signal sweep", file=sys.stderr)
        return EXIT_FAIL
    gui_main()
    return EXIT_OK


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    signal_help = "内置测试信号：" + "，".join("%s=%s" % (k, v[0]) for k, v in SIGNAL_INFO.items())
    p = argparse.ArgumentParser(
        prog="ds4_speaker_test",
        description="DS4 / DualSense 手柄扬声器测试工具（支持自定义测试音频）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--list", "-l", action="store_true", help="列出所有音频设备后退出")
    p.add_argument("--gui", action="store_true", help="打开图形界面（默认行为）")
    p.add_argument("--selftest", action="store_true", help="运行离线自检（默认不发声）")
    p.add_argument("--with-audio", action="store_true", help="自检时额外做一次静音播放预检")
    p.add_argument("--loopback-test", action="store_true",
                   help="回环自检：播放测试音频并用（手柄）麦克风录音，自动判定扬声器是否正常")

    # --- 蓝牙内置扬声器（HID + SBC）---
    p.add_argument("--bt", action="store_true",
                   help="走蓝牙 HID/SBC 通路，直接驱动手柄**内置扬声器**（需蓝牙连接）")
    p.add_argument("--bt-list", action="store_true", help="列出手柄 HID 接口及其连接方式（蓝牙/USB）")
    p.add_argument("--bt-selftest", action="store_true", help="蓝牙通路离线自检（不向手柄发送数据）")
    p.add_argument("--bt-path", type=lambda s: int(str(s), 0), default=OUTPUT_DEFAULT,
                   help="音频报告的输出通路字节：0x%02X=内置扬声器（默认），0x24=3.5mm 耳机孔"
                        % OUTPUT_DEFAULT)
    p.add_argument("--bt-bitrate", type=int, default=SBC_DEFAULT_BITRATE,
                   help="SBC 比特率；0（默认）= 跟随参考实现用 ffmpeg 默认参数")
    p.add_argument("--bt-frames", type=int, choices=[2, 4], default=4,
                   help="每条报告的 SBC 帧数（默认 4 = 0x17/462 字节，不足时自动用 0x14/270）")
    p.add_argument("--bt-exclusive", action="store_true", help="以独占方式打开手柄 HID")
    p.add_argument("--bt-lead", type=int, default=6,
                   help="开播前先连发几条报告把手柄缓冲垫起来，抗抖动（默认 6，0=关闭）")
    p.add_argument("--bt-speed", type=float, default=1.0,
                   help="送流速率倍数（默认 1.0=严格实时；仍断续可试 1.02 慢慢垫厚缓冲）")
    p.add_argument("--bt-control-every", type=int, default=20,
                   help="每 N 条音频报告插一条 0x11 刷新控制状态（默认 20，0=关闭）")
    p.add_argument("--bt-dry-run", action="store_true", help="只生成报告不写设备（用于检查参数）")
    p.add_argument("--bt-control-test", action="store_true",
                   help="用手柄灯条+震动验证 HID 通路是否打通（不依赖听）")
    p.add_argument("--bt-color", default="0,255,0", help="控制测试用的灯条颜色 R,G,B（默认 0,255,0）")
    p.add_argument("--bt-rumble", type=int, default=200, help="控制测试用的马达强度 0-255（默认 200）")
    p.add_argument("--bt-volume", type=int, default=255,
                   help="手柄内置扬声器音量 0-255（默认 255 = 100%%）")

    p.add_argument("--device", "-d", default=None,
                   help="输出设备：序号或名称子串，默认自动识别手柄扬声器")
    p.add_argument("--input-device", default=None,
                   help="回环测试的录音设备（默认电脑麦克风；插在手柄上的耳麦也可选）")
    p.add_argument("--signal", "-s", default="tone", help=signal_help)
    p.add_argument("--file", "-f", default=None, help="自定义测试音频文件（wav/mp3/flac/ogg/m4a...）")
    p.add_argument("--start", type=float, default=0.0, help="自定义音频的起始位置（秒）")
    p.add_argument("--duration", type=float, default=0.0, help="播放时长（秒，0=完整播放）")
    p.add_argument("--freq", type=float, default=1000.0, help="单音频率（Hz）")
    p.add_argument("--f0", type=float, default=20.0, help="扫频起始频率（Hz）")
    p.add_argument("--f1", type=float, default=20000.0, help="扫频终止频率（Hz）")
    p.add_argument("--channel", choices=["both", "left", "right"], default="both",
                   help="声道路由（测试单侧耳机 / 手柄内置喇叭）")
    p.add_argument("--volume-db", "-v", type=float, default=0.0, help="音量增益（dB，默认 0）")
    p.add_argument("--loop", action="store_true", help="循环播放")
    p.add_argument("--samplerate", type=int, default=0, help="播放采样率（0=设备默认）")
    p.add_argument("--channels", type=int, default=0, help="播放声道数（0=自动）")
    p.add_argument("--exclusive", action="store_true", help="使用 WASAPI 独占模式")
    p.add_argument("--blocksize", type=int, default=0, help="音频块大小（0=自动）")
    p.add_argument("--latency", type=float, default=None, help="期望延迟（秒）")
    p.add_argument("--save-rec", default=None, help="把回环测试录到的音频保存为 WAV")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    _setup_console()
    raw = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(raw)

    if args.list:
        return cmd_list(args)
    if args.bt_list:
        return cmd_bt_list(args)
    if args.bt_selftest:
        return cmd_bt_selftest(args)
    if args.bt_control_test:
        return cmd_bt_control_test(args)
    if args.bt and args.loopback_test:
        return cmd_bt_loopback(args)
    if args.bt:
        return cmd_bt_play(args)
    if args.selftest:
        return cmd_selftest(args)
    if args.loopback_test:
        return cmd_loopback(args)
    if args.gui:
        return cmd_gui(args)
    if not raw:
        return cmd_gui(args)  # 不带任何参数 → 打开图形界面
    return cmd_play(args)


if __name__ == "__main__":
    sys.exit(main())
