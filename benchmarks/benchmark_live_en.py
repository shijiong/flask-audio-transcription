"""Real-time Foundry Local benchmark using the unmodified app live-session path.

Example: selected-python benchmarks/benchmark_live_en.py --audio /path/to/input.mp3
Each repetition runs in a fresh process; no mock, HTTP, browser, or batch ASR.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import threading
import time
import traceback
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "flask-audio-transcription/src/foundry_backend.py"
ALIAS = "nemotron-speech-streaming-en-0.6b"
RATE = 16000
CHUNK_BYTES = 3200


def save(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def throttled() -> dict[str, Any]:
    try:
        raw = subprocess.check_output(["vcgencmd", "get_throttled"], text=True, timeout=10).strip()
        bits = int(raw.split("=", 1)[1], 16)
        return {"raw": raw, "bits": bits, "undervoltage_now": bool(bits & 1),
                "frequency_capped_now": bool(bits & 2), "throttled_now": bool(bits & 4),
                "soft_temperature_limit_now": bool(bits & 8),
                "undervoltage_occurred_since_boot": bool(bits & (1 << 16)),
                "frequency_cap_occurred_since_boot": bool(bits & (1 << 17)),
                "throttling_occurred_since_boot": bool(bits & (1 << 18)),
                "soft_temperature_limit_occurred_since_boot": bool(bits & (1 << 19))}
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return {"raw": None, "error": str(error)}


def telemetry() -> dict[str, Any]:
    status = {}
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith(("VmRSS:", "VmHWM:")):
            key, value = line.split(":", 1)
            status[key] = int(value.split()[0]) / 1024
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith(("MemAvailable:", "SwapFree:", "SwapTotal:")):
            key, value = line.split(":", 1)
            memory[key] = int(value.split()[0]) / 1024

    def number(path: str, divisor: int) -> float | None:
        try:
            return float(Path(path).read_text().strip()) / divisor
        except (OSError, ValueError):
            return None

    return {"monotonic_seconds": time.perf_counter(), "rss_mib": status.get("VmRSS"),
            "hwm_mib": status.get("VmHWM"), "memory_mib": memory,
            "temperature_c": number("/sys/class/thermal/thermal_zone0/temp", 1000),
            "cpu_mhz": number("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq", 1000),
            "load_average": list(os.getloadavg())}


def replay(backend: Any, model: Any, pcm: bytes, run: dict[str, Any]) -> None:
    # Import actual transcript assembly as well as the actual live session.
    from service import Job, apply_event

    job = Job("benchmark", "live", ALIAS, "en", Path("."))
    started = time.perf_counter()
    live = backend.live_session(model, "en")
    run["session_creation_seconds"] = time.perf_counter() - started
    events: list[dict[str, Any]] = []
    sends: list[dict[str, Any]] = []
    run["events"] = events
    run["chunks"] = sends
    ready, done = threading.Event(), threading.Event()
    reader_errors: list[str] = []
    clock: dict[str, float] = {}

    def consume() -> None:
        ready.set()
        try:
            for event in live.events():
                now = time.perf_counter()
                events.append({"monotonic_seconds": now, "text": event.text,
                               "final": event.final, "aggregate": event.aggregate})
                apply_event(job, event)
        except BaseException:
            reader_errors.append(traceback.format_exc())
        finally:
            clock["stream_end"] = time.perf_counter()
            done.set()

    reader = threading.Thread(target=consume, name="real-native-event-consumer", daemon=True)
    reader.start()
    ready.wait()
    try:
        clock["first_append"] = time.perf_counter()
        for index, offset in enumerate(range(0, len(pcm), CHUNK_BYTES)):
            target = clock["first_append"] + offset / (RATE * 2)
            # Absolute deadlines avoid accumulating timer drift. First chunk is
            # supplied at t=0; do not add trailing silence or pad the final chunk.
            done.wait(max(0, target - time.perf_counter()))
            if done.is_set():
                raise RuntimeError("Native event stream ended before finish(): " + "\n".join(reader_errors))
            before = time.perf_counter()
            live.append(pcm[offset:offset + CHUNK_BYTES])
            sends.append({"index": index, "bytes": min(CHUNK_BYTES, len(pcm) - offset),
                          "target_seconds": target - clock["first_append"],
                          "sent_seconds": before - clock["first_append"],
                          "lateness_seconds": max(0, before - target),
                          "append_seconds": time.perf_counter() - before})
        duration = len(pcm) / (RATE * 2)
        done.wait(max(0, clock["first_append"] + duration - time.perf_counter()))
        clock["finish_call"] = time.perf_counter()
        live.finish()
        clock["finish_return"] = time.perf_counter()
        if not done.wait(180):
            raise TimeoutError("Native stream did not drain within 180 seconds after finish()")
        reader.join()
        if reader_errors:
            raise RuntimeError("\n".join(reader_errors))
        if not job.text.strip() and not job.partial.strip():
            raise RuntimeError("The real model produced no non-empty transcript")
        for event in events:
            event["since_first_append_seconds"] = event["monotonic_seconds"] - clock["first_append"]
        partial = [e for e in events if e["text"].strip() and not e["final"] and not e["aggregate"]]
        final = [e for e in events if e["text"].strip() and e["final"] and not e["aggregate"]]
        text = [e for e in events if e["text"].strip()]
        run["stream"] = {
            "clock": clock, "audio_duration_seconds": duration,
            "input_seconds": clock["finish_call"] - clock["first_append"],
            "first_partial_seconds": partial[0]["since_first_append_seconds"] if partial else None,
            "first_final_seconds": final[0]["since_first_append_seconds"] if final else None,
            "first_nonempty_text_seconds": text[0]["since_first_append_seconds"] if text else None,
            "finish_call_seconds": clock["finish_return"] - clock["finish_call"],
            "finish_to_drained_seconds": clock["stream_end"] - clock["finish_call"],
            "finish_return_to_drained_seconds": clock["stream_end"] - clock["finish_return"],
            "total_seconds": clock["stream_end"] - clock["first_append"],
            "event_count": len(events), "partial_count": len(partial), "final_count": len(final),
            "aggregate_count": sum(e["aggregate"] for e in events),
            "chunk_count": len(sends), "bytes_sent": sum(s["bytes"] for s in sends),
            "maximum_send_lateness_seconds": max(s["lateness_seconds"] for s in sends),
            "median_send_lateness_seconds": statistics.median(s["lateness_seconds"] for s in sends),
            "transcript": job.text + job.partial,
        }
    finally:
        if not done.is_set():
            live.cancel()
            if not done.wait(15):
                # Do not release handles/backing buffers while inference still
                # uses them. Parent process timeout is the final safety net.
                raise RuntimeError("Cancellation did not stop native inference; process isolation required")
            reader.join()
        live.close()
        run["retained_audio_items_after_close"] = len(live._audio_items)


def worker(args: argparse.Namespace) -> int:
    run: dict[str, Any] = {"run": args.run, "pid": os.getpid(), "status": "error",
                           "started_at": datetime.now(timezone.utc).isoformat(),
                           "throttling_before": throttled(), "progress": []}
    samples = [telemetry()]
    stop = threading.Event()
    worker_start = time.perf_counter()

    def monitor() -> None:
        while not stop.wait(0.05):
            samples.append(telemetry())

    watcher = threading.Thread(target=monitor, daemon=True)
    watcher.start()
    backend = None
    try:
        sys.path.insert(0, str(BACKEND.parent))
        from foundry_backend import FoundryBackend

        pcm = (args.output / "input-16khz-mono-s16le.pcm").read_bytes()
        if not pcm or len(pcm) % 2:
            raise ValueError("Converted PCM must be nonempty and sample-aligned")
        backend = FoundryBackend()
        tick = time.perf_counter()
        manager = backend._initialize(lambda message: print(message, flush=True))
        run["sdk_initialization_seconds"] = time.perf_counter() - tick
        tick = time.perf_counter()
        candidate = manager.catalog.get_model(ALIAS)
        run["metadata_lookup_seconds"] = time.perf_counter() - tick
        if candidate is None:
            raise RuntimeError(f"Model {ALIAS} is absent from the real platform catalog")
        info = candidate.info
        run["model"] = {"alias": ALIAS, "id": info.id, "file_size_mb": info.file_size_mb,
                         "cached_before": candidate.is_cached,
                         "device": str(info.runtime.device_type) if info.runtime else None,
                         "execution_provider": info.runtime.execution_provider if info.runtime else None}
        loading: list[float] = []

        def progress(message: str) -> None:
            now = time.perf_counter()
            run["progress"].append({"seconds": now - worker_start, "message": message})
            if message == f"Loading {ALIAS}…":
                loading.append(now)
            print(message, flush=True)

        tick = time.perf_counter()
        # This is the production context manager, including its real download,
        # load and unload calls. No replacing/monkeypatching SDK methods.
        with backend.model(ALIAS, progress) as model:
            loaded = time.perf_counter()
            run["model_context_ready_seconds"] = loaded - tick
            run["model_load_seconds"] = loaded - loading[0] if loading else None
            run["context_before_load_seconds"] = loading[0] - tick if loading else None
            run["model"]["cache_path"] = model.get_path()
            run["model"]["cached_after"] = model.is_cached
            replay(backend, model, pcm, run)
            run["status"] = "ok"
    except Exception as error:
        run["error"] = str(error)
        run["traceback"] = traceback.format_exc()
        traceback.print_exc()
    finally:
        if backend is not None and backend._manager is not None:
            backend._manager.close()
        samples.append(telemetry())
        stop.set()
        watcher.join()
        run["telemetry_samples"] = samples
        run["peak_process_rss_mib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        run["sampled_peak_process_rss_mib"] = max(s["rss_mib"] or 0 for s in samples)
        temperatures = [s["temperature_c"] for s in samples if s["temperature_c"] is not None]
        run["max_temperature_c"] = max(temperatures) if temperatures else None
        run["throttling_after"] = throttled()
        run["worker_elapsed_seconds"] = time.perf_counter() - worker_start
        save(args.output / f"run-{args.run:02d}.json", run)
    print(json.dumps({k: v for k, v in run.items() if k in {
        "run", "status", "model_load_seconds", "peak_process_rss_mib", "max_temperature_c", "error"}},
        ensure_ascii=False), flush=True)
    return 0 if run["status"] == "ok" else 1


def stats(values: list[float | None]) -> dict[str, Any]:
    observed = [v for v in values if v is not None]
    return {"observed_runs": len(observed), "total_runs": len(values),
            "median": statistics.median(observed) if observed else None,
            "min": min(observed) if observed else None, "max": max(observed) if observed else None}


def write_report(output: Path, data: dict[str, Any]) -> None:
    summary, runs, metadata = data["summary"], data["runs"], data["metadata"]

    def display(key: str, unit: str = "s") -> str:
        item = summary[key]
        if item["median"] is None:
            return "未观察到（0/3 次；不是 0 ms）"
        return f"{item['median']:.3f} {unit}（{item['min']:.3f}–{item['max']:.3f}）"

    model = runs[0]["model"]
    flags = [r[k].get("bits") for r in runs for k in ("throttling_before", "throttling_after")]
    throttle_text = "否；3 次运行前后均为 0x0" if all(v == 0 for v in flags) else "见逐次 vcgencmd 原始状态；历史位不一定由本次测试触发"
    lines = [
        "# 实时英语转录 benchmark：真实 Foundry Local live session", "",
        f"测试日期：{metadata['started_at']}。真实音频、真实模型、每次 fresh process，共 {len(runs)} 次。", "",
        "## 可直接回填文章的表格", "",
        "下列缓存后时间为 3 次中位数，括号为观测最小值–最大值；内存和温度取 3 次最大值。", "",
        "| 模型 | 下载大小 | 首次加载 | 缓存后加载 | 首字延迟（partial） | 确认延迟（first final） | 峰值内存 | 最高温度 | 是否降频/欠压 |",
        "|---|---:|---|---|---|---|---:|---:|---|",
        f"| {ALIAS} | {model['file_size_mb']} MB（catalog） | {runs[0]['model_load_seconds']:.3f} s（本次首轮；{'已缓存' if model['cached_before'] else '首次下载后'}） | {display('cached_load_seconds')} | {display('first_partial_seconds')} | {display('first_final_seconds')} | {summary['peak_process_rss_mib']['max']:.1f} MiB | {summary['max_temperature_c']['max']:.2f} °C | {throttle_text} |", "",
        "## 环境与输入", "",
        f"- 设备：{metadata['device']}；{metadata['cpu_count']} 核；{metadata['platform']}。",
        f"- Python：{metadata['python']}；Foundry Local SDK：{metadata['sdk_version']}。",
        f"- 实际 model id：{model['id']}；执行提供器：{model['execution_provider']}。",
        f"- 输入：{metadata['audio_path']}；SHA-256：{metadata['input_sha256']}。",
        f"- 原始音频：{metadata['audio_probe']['streams'][0]['sample_rate']} Hz、{metadata['audio_probe']['streams'][0]['channels']} 声道；容器时长 {metadata['source_duration_seconds']:.6f} s。",
        f"- ffmpeg 解码、下混和重采样为 16 kHz / mono / signed PCM16LE，实际送入 {metadata['pcm_bytes']} 字节、{metadata['pcm_duration_seconds']:.6f} s；转换耗时 {metadata['preprocess_seconds']:.3f} s，不计入模型/流式计时。",
        "- 转换过程使用 ffmpeg/libswresample，输出格式与应用兼容，但并非声称与浏览器 AudioWorklet 使用相同重采样算法。没有补尾部静音、没有裁剪或替换输入。",
        "- 每块 3200 字节 = 100 ms，末块按实际剩余采样数发送；首块 t=0 入队，后续按绝对 100 ms 截止时间发送，音频全部播放时长届满后 finish()。",
        "- 真实路径：FoundryBackend.model() → FoundryBackend.live_session() → NativeLiveSession.append()/finish()/events()/close()；转录拼接复用 service.apply_event()。",
        "- 没有 mock，没有批量 Whisper API，没有浏览器/Flask HTTP，也没有摘要模型。", "",
        "## 指标定义与汇总", "",
        "| 指标 | 中位数（观测范围） |", "|---|---|",
    ]
    for label, key, unit in [
        ("已缓存模型加载", "cached_load_seconds", "s"),
        ("首个非空 partial", "first_partial_seconds", "s"),
        ("首个非空 confirmed/final（排除 aggregate）", "first_final_seconds", "s"),
        ("首个任意非空文本（不等同 partial）", "first_nonempty_text_seconds", "s"),
        ("finish 调用至流完全排空", "finish_to_drained_seconds", "s"),
        ("第一块 append 至完整流结束", "total_seconds", "s"),
        ("每轮最大送块迟到", "maximum_send_lateness_seconds", "s"),
        ("峰值进程 RSS", "peak_process_rss_mib", "MiB"),
        ("每轮最高采样温度", "max_temperature_c", "°C"),
    ]:
        lines.append(f"| {label} | {display(key, unit)} |")
    lines += ["", "## 每次运行", "",
              "| run / PID | load s | partial s | final s | finish→drain s | total s | peak RSS MiB | max °C | partial/final/aggregate | throttle 前→后 |",
              "|---|---:|---|---:|---:|---:|---:|---:|---|---|"]
    for r in runs:
        stream = r["stream"]
        partial = f"{stream['first_partial_seconds']:.3f}" if stream["first_partial_seconds"] is not None else "未观察到"
        final = f"{stream['first_final_seconds']:.3f}" if stream["first_final_seconds"] is not None else "未观察到"
        lines.append(f"| {r['run']} / {r['pid']} | {r['model_load_seconds']:.3f} | {partial} | {final} | {stream['finish_to_drained_seconds']:.3f} | {stream['total_seconds']:.3f} | {r['peak_process_rss_mib']:.1f} | {r['max_temperature_c']:.2f} | {stream['partial_count']}/{stream['final_count']}/{stream['aggregate_count']} | {r['throttling_before']['raw']} → {r['throttling_after']['raw']} |")
    lines += ["", "## 重要边界", "",
              "- 首次加载是本次 benchmark 首轮真实 load，不是凭空声称清空下载缓存后的冷启动。若 cached_before=true，真正无缓存首次下载/加载未测；脚本不会删除用户缓存或全局 drop_caches。",
              "- 加载计时通过真实 model() 的 ‘Loading …’ progress 回调至 with 成功进入，近似 model.load()，未替换 SDK 方法；SDK 初始化、目录查找、下载/缓存检查在其外。回调打印有极小测量开销。",
              "- file_size_mb 是模型目录元数据的下载大小声明，不是本次网络传输量，也不是内存占用；两者不能混用。",
              "- partial 必须是非空、event.final=false 且 aggregate=false；first final 必须是非空、event.final=true 且 aggregate=false。遵从仓库 copy_segment() 的真实事件标记，不把 confirmed token 改称 partial。",
              "- 如果 partial 为 null，表示该模型/配置/素材没有可观察的 partial 事件，不能补成首字延迟；另列 first_nonempty_text 便于说明真实可见首字。",
              "- 起点为第一块 append 调用前；流结束点为 events() 完整迭代结束（包括 final_response 释放），不含 session.close/model unload、加载或预处理。finish→drain 从 finish() 调用前计算。",
              "- RSS 为 Linux resource.getrusage(RUSAGE_SELF).ru_maxrss / 1024，包括 Python、native runtime、模型、工作区及应用保留的 PCM 缓冲，不是模型净内存。50 ms 采样同时保留 /proc RSS 与温度。",
              "- get_throttled 前后保留完整原始字符串与当前/历史位解码；历史标志可能来自启动后更早事件。最高温度是 50 ms 采样最大值而非绝对硬件峰值。",
              "- fresh process 不等于冷 OS 文件缓存；桌面/VS Code 后台负载未停止。短音频、3 次运行不能代表长会话稳态或准确率。", "",
              "## 复现", "",
              f"运行环境：{sys.executable}", "",
              "```bash",
              f"{sys.executable} benchmarks/benchmark_live_en.py --audio /home/pi/Documents/Recording.mp3 --output benchmarks/results/live-en-<new-date-or-run> --repeats 3",
              "```", "",
              "脚本拒绝覆盖已有 results.json。请为复测指定新的结果目录。需要 ffmpeg、ffprobe、vcgencmd 和已安装的真实 Foundry Local SDK。输入不存在会立即失败，不会生成替代音频。",
              "原始产物：results.json、run-01/02/03.json（含完整事件、送块时间、progress、telemetry）、每轮日志和 transcript，以及标准化 PCM。", ""]
    (output / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", type=Path, default=Path("/home/pi/Documents/Recording.mp3"))
    parser.add_argument("--output", type=Path, default=ROOT / "benchmarks/results" / f"live-en-{datetime.now().date()}")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--run", type=int, default=1, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.audio.is_file():
        parser.error(f"指定音频不存在：{args.audio}；请提供真实音频路径")
    args.audio, args.output = args.audio.resolve(), args.output.resolve()
    if args.worker:
        return worker(args)
    if args.repeats < 3:
        parser.error("至少需要 3 次 fresh-process 重复")
    if (args.output / "results.json").exists():
        parser.error("结果目录已有 results.json；请使用新目录以保留原始测量")
    args.output.mkdir(parents=True, exist_ok=True)
    probe = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "a:0", "-show_streams", "-show_format", "-of", "json", str(args.audio)
    ], text=True))
    pcm_path = args.output / "input-16khz-mono-s16le.pcm"
    conversion = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(args.audio),
                  "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(RATE), "-c:a", "pcm_s16le", "-f", "s16le", str(pcm_path)]
    tick = time.perf_counter()
    subprocess.run(conversion, check=True, timeout=120)
    preprocessing = time.perf_counter() - tick
    device = Path("/proc/device-tree/model")
    metadata = {
        "started_at": datetime.now(timezone.utc).isoformat(), "audio_path": str(args.audio),
        "input_sha256": sha256(args.audio), "pcm_sha256": sha256(pcm_path), "audio_probe": probe,
        "source_duration_seconds": float(probe["format"]["duration"]),
        "pcm_bytes": pcm_path.stat().st_size, "pcm_duration_seconds": pcm_path.stat().st_size / (RATE * 2),
        "preprocess_seconds": preprocessing, "preprocess_command": conversion,
        "ffmpeg_version": subprocess.check_output(["ffmpeg", "-version"], text=True).splitlines()[0],
        "device": device.read_text().rstrip("\0") if device.exists() else platform.machine(),
        "cpu_count": os.cpu_count(), "platform": platform.platform(), "python": sys.version.splitlines()[0],
        "sdk_version": version("foundry-local-sdk"), "backend_path": str(BACKEND),
        "backend_sha256": sha256(BACKEND), "harness_sha256": sha256(Path(__file__)),
        "method": "Fresh process, real app model/live_session/append/finish/events/close; 100 ms PCM at real-time deadlines",
    }
    save(args.output / "metadata.json", metadata)
    runs = []
    for index in range(1, args.repeats + 1):
        print(f"=== Real native live run {index}/{args.repeats} ===", flush=True)
        command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--run", str(index),
                   "--audio", str(args.audio), "--output", str(args.output)]
        with (args.output / f"run-{index:02d}.log").open("w", encoding="utf-8") as log:
            try:
                completed = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=600)
                exit_code = completed.returncode
            except subprocess.TimeoutExpired:
                exit_code = 124
        print((args.output / f"run-{index:02d}.log").read_text(), flush=True)
        path = args.output / f"run-{index:02d}.json"
        if not path.exists():
            save(path, {"run": index, "status": "error", "error": f"Worker exited {exit_code} without producing JSON"})
        run = json.loads(path.read_text())
        run["exit_code"] = exit_code
        save(path, run)
        runs.append(run)
        save(args.output / "results.json", {"metadata": metadata, "runs": runs})
        if exit_code or run["status"] != "ok":
            print(f"真实 SDK 测试失败；原始错误已保留在 {path}，停止后续测试。", file=sys.stderr)
            return 1
        (args.output / f"run-{index:02d}-transcript.txt").write_text(run["stream"]["transcript"] + "\n", encoding="utf-8")
    summary = {
        "first_observed_load_seconds": runs[0]["model_load_seconds"],
        "truly_uncached_first_load_measured": not runs[0]["model"]["cached_before"],
        "cached_load_seconds": stats([r["model_load_seconds"] for r in runs if r["model"]["cached_before"]]),
        "peak_process_rss_mib": stats([r["peak_process_rss_mib"] for r in runs]),
        "max_temperature_c": stats([r["max_temperature_c"] for r in runs]),
    }
    for key in ("first_partial_seconds", "first_final_seconds", "first_nonempty_text_seconds",
                "finish_to_drained_seconds", "total_seconds", "maximum_send_lateness_seconds"):
        summary[key] = stats([r["stream"][key] for r in runs])
    data = {"metadata": metadata, "runs": runs, "summary": summary}
    save(args.output / "results.json", data)
    write_report(args.output, data)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())