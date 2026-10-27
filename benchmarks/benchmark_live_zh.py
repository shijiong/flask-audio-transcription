"""Real Chinese live ASR benchmark, using the repository's native session.

No mock, HTTP, browser, batch transcription, transcript cleanup, or fake input.
Three sequential fresh processes by default; source WAV and native text retained.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import re
import resource
import statistics
import subprocess
import sys
import threading
import time
import traceback
from typing import Any
import wave

from benchmark_live_en import save, sha256, stats, telemetry, throttled


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "flask-audio-transcription/src/foundry_backend.py"
ALIAS = "nemotron-3.5-asr-streaming-0.6b"
RATE, CHUNK_BYTES = 16000, 3200
TAG = re.compile(r"</?[a-z]{2,3}(?:-[a-z0-9]{2,8})*>", re.IGNORECASE)
HAN = re.compile(r"[\u3400-\u9fff]")


def tag_review(events: list[dict[str, Any]], transcript: str) -> dict[str, Any]:
    # Concatenate before matching: tags can be split over several token events.
    streamed = "".join(e["text"] for e in events if not e["aggregate"])
    final_tags = TAG.findall(transcript)
    stream_tags = TAG.findall(streamed)
    return {
        "regex": TAG.pattern, "tags_in_final_text": final_tags,
        "tags_in_concatenated_stream": stream_tags,
        "unique_tags": sorted(set(final_tags + stream_tags)),
        "final_contains_language_tags": bool(final_tags),
        "stream_contains_language_tags": bool(stream_tags),
        "chinese_characters_in_final": len(HAN.findall(transcript)),
        "raw_final_prefix": transcript[:240],
        "text_was_cleaned": False,
    }


def replay(backend: Any, model: Any, pcm: bytes, language: str, run: dict[str, Any]) -> None:
    from service import Job, apply_event

    job = Job("benchmark-zh", "live", ALIAS, language, Path("."))
    tick = time.perf_counter()
    live = backend.live_session(model, language)
    run["session_creation_seconds"] = time.perf_counter() - tick
    events: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []
    run["events"], run["chunks"] = events, chunks
    ready, done = threading.Event(), threading.Event()
    failures: list[str] = []
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
            failures.append(traceback.format_exc())
        finally:
            clock["stream_end"] = time.perf_counter()
            done.set()

    reader = threading.Thread(target=consume, name="real-chinese-native-events", daemon=True)
    reader.start()
    ready.wait()
    try:
        clock["first_append"] = time.perf_counter()
        for index, offset in enumerate(range(0, len(pcm), CHUNK_BYTES)):
            target = clock["first_append"] + offset / (RATE * 2)
            done.wait(max(0, target - time.perf_counter()))
            if done.is_set():
                raise RuntimeError("Real native stream ended before finish(): " + "\n".join(failures))
            before = time.perf_counter()
            live.append(pcm[offset:offset + CHUNK_BYTES])
            chunks.append({"index": index, "bytes": min(CHUNK_BYTES, len(pcm) - offset),
                           "target_seconds": offset / (RATE * 2),
                           "sent_seconds": before - clock["first_append"],
                           "lateness_seconds": max(0, before - target),
                           "append_seconds": time.perf_counter() - before})
        duration = len(pcm) / (RATE * 2)
        done.wait(max(0, clock["first_append"] + duration - time.perf_counter()))
        clock["finish_call"] = time.perf_counter()
        live.finish()
        clock["finish_return"] = time.perf_counter()
        if not done.wait(180):
            raise TimeoutError("Real stream failed to drain within 180 seconds of finish()")
        reader.join()
        if failures:
            raise RuntimeError("\n".join(failures))
        for event in events:
            event["since_first_append_seconds"] = event["monotonic_seconds"] - clock["first_append"]
        partial = [e for e in events if e["text"].strip() and not e["final"] and not e["aggregate"]]
        confirmed = [e for e in events if e["text"].strip() and e["final"] and not e["aggregate"]]
        text_events = [e for e in events if e["text"].strip()]
        chinese_events = [e for e in events if HAN.search(e["text"])]
        transcript = job.text + job.partial
        run["stream"] = {
            "clock": clock, "audio_duration_seconds": duration,
            "input_seconds": clock["finish_call"] - clock["first_append"],
            "first_partial_seconds": partial[0]["since_first_append_seconds"] if partial else None,
            "first_final_seconds": confirmed[0]["since_first_append_seconds"] if confirmed else None,
            "first_nonempty_text_seconds": text_events[0]["since_first_append_seconds"] if text_events else None,
            "first_chinese_character_seconds": chinese_events[0]["since_first_append_seconds"] if chinese_events else None,
            "finish_call_seconds": clock["finish_return"] - clock["finish_call"],
            "finish_to_drained_seconds": clock["stream_end"] - clock["finish_call"],
            "finish_return_to_drained_seconds": clock["stream_end"] - clock["finish_return"],
            "total_seconds": clock["stream_end"] - clock["first_append"],
            "partial_count": len(partial), "final_count": len(confirmed),
            "aggregate_count": sum(e["aggregate"] for e in events), "event_count": len(events),
            "chunk_count": len(chunks), "bytes_sent": sum(c["bytes"] for c in chunks),
            "maximum_send_lateness_seconds": max(c["lateness_seconds"] for c in chunks),
            "median_send_lateness_seconds": statistics.median(c["lateness_seconds"] for c in chunks),
            "transcript": transcript,
        }
        run["language_tag_review"] = tag_review(events, transcript)
        if not transcript.strip():
            raise RuntimeError("Real native model returned no text")
        if not HAN.search(transcript):
            raise RuntimeError("Real output contains no Chinese characters; cannot report a usable Chinese result")
    finally:
        if not done.is_set():
            live.cancel()
            if not done.wait(15):
                raise RuntimeError("Native cancellation timed out; handles cannot be safely released")
            reader.join()
        live.close()
        run["retained_audio_items_after_close"] = len(live._audio_items)


def worker(args: argparse.Namespace) -> int:
    run: dict[str, Any] = {"run": args.run, "pid": os.getpid(), "language": args.language,
                           "status": "error", "started_at": datetime.now(timezone.utc).isoformat(),
                           "throttling_before": throttled(), "progress": []}
    start = time.perf_counter()
    samples = [telemetry()]
    stop = threading.Event()

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
            raise ValueError("PCM is empty or not sample-aligned")
        backend = FoundryBackend()
        tick = time.perf_counter()
        manager = backend._initialize(lambda message: print(message, flush=True))
        run["sdk_initialization_seconds"] = time.perf_counter() - tick
        tick = time.perf_counter()
        candidate = manager.catalog.get_model(ALIAS)
        run["metadata_lookup_seconds"] = time.perf_counter() - tick
        if candidate is None:
            raise RuntimeError(f"Real catalog does not contain {ALIAS}")
        info = candidate.info
        run["model"] = {"alias": ALIAS, "id": info.id, "file_size_mb": info.file_size_mb,
                         "cached_before": candidate.is_cached,
                         "execution_provider": info.runtime.execution_provider if info.runtime else None,
                         "device": str(info.runtime.device_type) if info.runtime else None}
        loading: list[float] = []

        def progress(message: str) -> None:
            now = time.perf_counter()
            run["progress"].append({"seconds": now - start, "message": message})
            if message == f"Loading {ALIAS}…":
                loading.append(now)
            print(message, flush=True)

        tick = time.perf_counter()
        with backend.model(ALIAS, progress) as model:
            loaded = time.perf_counter()
            run["model_context_ready_seconds"] = loaded - tick
            run["model_load_seconds"] = loaded - loading[0] if loading else None
            run["context_before_load_seconds"] = loading[0] - tick if loading else None
            run["model"]["cached_after"] = model.is_cached
            run["model"]["cache_path"] = model.get_path()
            replay(backend, model, pcm, args.language, run)
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
        run["worker_elapsed_seconds"] = time.perf_counter() - start
        save(args.output / f"run-{args.run:02d}.json", run)
    print(json.dumps({k: v for k, v in run.items() if k in {
        "run", "status", "model_load_seconds", "peak_process_rss_mib", "max_temperature_c", "error"}},
        ensure_ascii=False), flush=True)
    return 0 if run["status"] == "ok" else 1


def report(output: Path, data: dict[str, Any]) -> None:
    metadata, runs, summary = data["metadata"], data["runs"], data["summary"]
    model = runs[0]["model"]

    def display(key: str, unit: str = "s") -> str:
        item = summary[key]
        if item["median"] is None:
            return f"未观察到（0/{len(runs)} 次；不是 0 ms）"
        return f"{item['median']:.3f} {unit}（{item['min']:.3f}–{item['max']:.3f}）"

    bits = [r[k].get("bits") for r in runs for k in ("throttling_before", "throttling_after")]
    throttle = "否；前后均 0x0" if all(b == 0 for b in bits) else "见逐轮当前/历史位，不能推测"
    tags = summary["unique_language_tags"]
    tag_text = ", ".join(f"`{tag}`" for tag in tags) if tags else "未观察到语言标签"
    rows = [
        "# 实时中文转录 benchmark：真实 Foundry Local SDK", "",
        f"日期：{metadata['started_at']}；语言：`{metadata['language']}`；{len(runs)} 次 fresh process，全部成功。", "",
        "## 文章简表", "",
        "时间为中位数（观测范围），内存/温度取三次最大值。首次加载单列本次第 1 轮，不虚构无缓存冷启动。", "",
        "| 模型 / 语言 | 下载大小 | 首次加载 | 缓存后加载 | first partial | first final/confirmed | 峰值 RSS | 最高温度 | 降频/欠压 | 语言标签 |",
        "|---|---:|---|---|---|---|---:|---:|---|---|",
        f"| {ALIAS} / {metadata['language']} | {model['file_size_mb']} MB（catalog） | {runs[0]['model_load_seconds']:.3f} s（本次首轮；{'已缓存' if model['cached_before'] else '下载后'}） | {display('cached_load_seconds')} | {display('first_partial_seconds')} | {display('first_final_seconds')} | {summary['peak_process_rss_mib']['max']:.1f} MiB | {summary['max_temperature_c']['max']:.2f} °C | {throttle} | {tag_text} |", "",
        "## 环境、素材与真实路径", "",
        f"- 硬件：{metadata['device']}，{metadata['cpu_count']} 核；{metadata['platform']}。",
        f"- Python {metadata['python']}；Foundry Local SDK {metadata['sdk_version']}；{model['execution_provider']}。",
        f"- model id：`{model['id']}`；alias：`{model['alias']}`；`file_size_mb={model['file_size_mb']}`。",
        f"- 输入：{metadata['audio_path']}；SHA-256：{metadata['input_sha256']}。",
        f"- 输入音频头：{json.dumps(metadata['wave_header'], ensure_ascii=False)}。",
        f"- 处理：{metadata['preprocess_method']}；{metadata['pcm_bytes']} 字节 PCM，{metadata['pcm_duration_seconds']:.6f} s；预处理耗时 {metadata['preprocess_seconds']:.3f} s，未计入流式或 load。",
        "- 使用 FoundryBackend.model() 的真实下载/加载/卸载，并使用 live_session() 创建 NativeLiveSession。append()/finish()/events()/close() 均为仓库原实现；拼接使用 service.apply_event()。没有 mock、batch API、浏览器、HTTP 或摘要。",
        "- 每 3200 字节 = 100 ms，首块 t=0，按绝对截止时间实时发送，最后不足 100 ms 原样发送；到完整音频时长后 finish。不补尾部静音，不裁剪、不循环音频。",
        "- 首次加载是本次首轮 load；cached_before=true 时，真正无缓存下载/冷加载未测。缓存后中位数包括所有 cached_before=true 的轮次，不清理用户缓存或 OS 页缓存。",
        "- load 计时以真实 model() 的 Loading progress 回调为开始、with 成功进入为结束，近似 model.load()；初始化/目录查询/下载与缓存检查另记，不包含其中。", "",
        "## 延迟与流式总耗时", "", "| 指标 | 中位数（最小–最大） |", "|---|---|",
    ]
    for label, key, unit in [
        ("首个非空 partial", "first_partial_seconds", "s"),
        ("首个应用 confirmed（排除 aggregate）", "first_final_seconds", "s"),
        ("首个非空文本（可能只是语言标签）", "first_nonempty_text_seconds", "s"),
        ("首个包含汉字的事件", "first_chinese_character_seconds", "s"),
        ("finish 至完全排空", "finish_to_drained_seconds", "s"),
        ("第一块 append 至流结束", "total_seconds", "s"),
        ("每轮最大送块迟到", "maximum_send_lateness_seconds", "s"),
        ("每轮 RSS 峰值", "peak_process_rss_mib", "MiB"),
        ("每轮温度峰值", "max_temperature_c", "°C"),
        ("目录查询（不含 load）", "metadata_lookup_seconds", "s"),
    ]:
        rows.append(f"| {label} | {display(key, unit)} |")
    rows += ["", "## 原始逐轮结果", "",
             "| run / PID | load s | partial s | confirmed s | 首汉字 s | drain s | total s | RSS MiB | °C | partial/confirmed/aggregate | throttle 前→后 |",
             "|---|---:|---|---|---|---:|---:|---:|---:|---|---|"]
    for r in runs:
        s = r["stream"]

        def latency(key: str) -> str:
            return f"{s[key]:.3f}" if s[key] is not None else "未观察到"

        rows.append(f"| {r['run']} / {r['pid']} | {r['model_load_seconds']:.3f} | {latency('first_partial_seconds')} | {latency('first_final_seconds')} | {latency('first_chinese_character_seconds')} | {s['finish_to_drained_seconds']:.3f} | {s['total_seconds']:.3f} | {r['peak_process_rss_mib']:.1f} | {r['max_temperature_c']:.2f} | {s['partial_count']}/{s['final_count']}/{s['aggregate_count']} | {r['throttling_before']['raw']} → {r['throttling_after']['raw']} |")
    rows += ["", "## 语言标签与中文结果解读", "",
             f"- 所有原始事件和拼接文本均未去标签、未改写。检测最终文本和完整 token 流（先拼接，避免漏检跨事件标签）；观察到：{tag_text}。",
             f"- 最终文本带标签的轮次：{summary['runs_with_tags_in_final']}/{len(runs)}；token 流带标签的轮次：{summary['runs_with_tags_in_stream']}/{len(runs)}。",
             f"- 各轮最终文本汉字数：{[r['language_tag_review']['chinese_characters_in_final'] for r in runs]}。包含汉字只证明有中文输出，不是识别准确率验证。",
             "- 如果标签存在，建议文章明确提及：原生多语言输出可能夹带语言标记，不能当作用户口述内容；标签可能比真正的中文内容更早出现。表中的 first final 是原始事件口径，首个包含汉字的延迟另列，避免把标签延迟误称为中文首字。",
             "- 语言标签名称不能单独作为识别语言或准确率的证据；即使出现 `<en-US>`，也要结合实际中文文本判断。本测试不在生产代码中过滤标签。",
             "- 没有人工参考或直接听审，不能提供 CER/WER 或保证专有词正确。`auto` 为可选，本轮只实测主场景 zh-CN，不声称测过 auto。", "",
             "### 原始输出示例（run 1，未清理）", "", "```text", runs[0]["stream"]["transcript"], "```", "",
             "## 事件及计量边界", "",
             "- partial = 非空且 event.final=false、aggregate=false。confirmed = 非空且 event.final=true、aggregate=false。copy_segment() 将 native kind!=1 映射为 final=True；NONE=0 和 FINAL=2 均可进入应用 confirmed。因此 first final 不是 native FINAL=2 的保证，也不是全文完成。",
             "- 第一块 append 调用前开始计时，events() 迭代和 final_response 释放全部结束为终点。finish→drain 包含 finish 调用，不含 close/unload。",
             "- 所有原始语言标签算作非空输出，但另计首个含汉字事件。不观察到 partial 时 JSON 为 null，不替换为 confirmed，也不是 0 秒。",
             "- RSS 为 ru_maxrss/1024 的全进程峰值，包含 Python/SDK/native/模型/工作区/保留音频，不是模型净内存。温度与 /proc RSS 每 50 ms 采样；温度为采样峰值。",
             "- get_throttled 保存完整原文和位解码；历史位可来自此前系统事件，不应直接归因本轮。后台桌面负载未关，fresh process 不等于 OS 冷页缓存。",
             "- 目录网络失败/区域回退或 SDK 警告原文在 run-XX.log；如果发生，查询耗时已单列，不能将约几秒 load 误写成应用完整启动时间。", "",
             "## 复现与文件", "",
             "```bash", f"{sys.executable} benchmarks/benchmark_live_zh.py --audio /home/pi/Documents/output_16k_mono.wav --language zh-CN --repeats 3 --output benchmarks/results/live-zh-recheck", "```", "",
             "需要真实 SDK、ffprobe，以及当前输入兼容 Python wave 的 PCM WAV。脚本拒绝覆盖已有 results.json；音频缺失/格式不兼容/模型不存在/SDK 失败会报错并保留失败，不生成替代输入或结果。",
             "产物：metadata.json、results.json、REPORT.md、每轮 run-XX.json（全部事件/送块/telemetry/progress）、run-XX.log、run-XX-transcript.txt、从原 WAV 无损提取的 PCM。元数据记录输入、PCM、backend、service、脚本和复用计量工具的哈希。", ""]
    (output / "REPORT.md").write_text("\n".join(rows), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", type=Path, default=Path("/home/pi/Documents/output_16k_mono.wav"))
    parser.add_argument("--language", choices=["zh-CN", "auto"], default="zh-CN")
    parser.add_argument("--output", type=Path, default=ROOT / "benchmarks/results" / f"live-zh-{datetime.now().date()}")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--run", type=int, default=1, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.audio.is_file():
        parser.error(f"指定音频缺失：{args.audio}；请提供真实音频路径")
    args.audio, args.output = args.audio.resolve(), args.output.resolve()
    if args.worker:
        return worker(args)
    if args.repeats < 3:
        parser.error("至少需要 3 次 fresh-process 重复")
    if (args.output / "results.json").exists():
        parser.error("已有 results.json；请选择新的结果目录，保留原始实测数据")
    probe = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "a:0", "-show_streams", "-show_format", "-of", "json", str(args.audio)
    ], text=True))
    tick = time.perf_counter()
    with wave.open(str(args.audio), "rb") as source:
        header = {"sample_rate": source.getframerate(), "channels": source.getnchannels(),
                  "sample_width_bytes": source.getsampwidth(), "compression": source.getcomptype(),
                  "frames": source.getnframes()}
        if (header["sample_rate"], header["channels"], header["sample_width_bytes"], header["compression"]) != (RATE, 1, 2, "NONE"):
            parser.error(f"输入不是 16kHz mono PCM16 WAV：{header}；不会静默替换素材")
        pcm = source.readframes(source.getnframes())
    if len(pcm) != header["frames"] * 2 or not pcm:
        parser.error("原始 WAV 的 PCM 帧数量不完整或为空")
    preprocessing = time.perf_counter() - tick
    args.output.mkdir(parents=True, exist_ok=True)
    pcm_path = args.output / "input-16khz-mono-s16le.pcm"
    pcm_path.write_bytes(pcm)
    device = Path("/proc/device-tree/model")
    metadata = {
        "started_at": datetime.now(timezone.utc).isoformat(), "audio_path": str(args.audio),
        "language": args.language, "model_alias": ALIAS,
        "input_sha256": sha256(args.audio), "pcm_sha256": sha256(pcm_path),
        "audio_probe": probe, "wave_header": header, "pcm_bytes": len(pcm),
        "pcm_duration_seconds": len(pcm) / (RATE * 2), "preprocess_seconds": preprocessing,
        "preprocess_method": "Python wave.readframes: extract exact original 16kHz mono PCM16LE, discard WAV container only; no resample, trim, gain, or added silence",
        "device": device.read_text().rstrip("\0") if device.exists() else platform.machine(),
        "cpu_count": os.cpu_count(), "platform": platform.platform(), "python": sys.version.splitlines()[0],
        "sdk_version": version("foundry-local-sdk"), "backend_path": str(BACKEND),
        "backend_sha256": sha256(BACKEND), "service_sha256": sha256(BACKEND.parent / "service.py"),
        "harness_sha256": sha256(Path(__file__)),
        "shared_telemetry_helpers_sha256": sha256(Path(__file__).with_name("benchmark_live_en.py")),
    }
    save(args.output / "metadata.json", metadata)
    runs = []
    for index in range(1, args.repeats + 1):
        print(f"=== Real {ALIAS} / {args.language} run {index}/{args.repeats} ===", flush=True)
        command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--run", str(index),
                   "--audio", str(args.audio), "--language", args.language, "--output", str(args.output)]
        with (args.output / f"run-{index:02d}.log").open("w", encoding="utf-8") as log:
            try:
                completed = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=900)
                exit_code = completed.returncode
            except subprocess.TimeoutExpired:
                exit_code = 124
        print((args.output / f"run-{index:02d}.log").read_text(), flush=True)
        path = args.output / f"run-{index:02d}.json"
        if not path.exists():
            save(path, {"run": index, "status": "error", "error": f"Worker exit {exit_code}; no JSON produced"})
        run = json.loads(path.read_text())
        run["exit_code"] = exit_code
        save(path, run)
        runs.append(run)
        save(args.output / "results.json", {"metadata": metadata, "runs": runs})
        if exit_code or run["status"] != "ok":
            (args.output / "REPORT.md").write_text(
                f"# 实时中文 benchmark 未完成\n\n第 {index} 轮真实执行失败：{run.get('error')}\n\n已完成原始数据保留在 results.json 和 run-XX.json/log；未编造缺失结果。\n", encoding="utf-8")
            return 1
        (args.output / f"run-{index:02d}-transcript.txt").write_text(run["stream"]["transcript"] + "\n", encoding="utf-8")
    summary = {
        "first_observed_load_seconds": runs[0]["model_load_seconds"],
        "truly_uncached_first_load_measured": not runs[0]["model"]["cached_before"],
        "cached_load_seconds": stats([r["model_load_seconds"] for r in runs if r["model"]["cached_before"]]),
        "peak_process_rss_mib": stats([r["peak_process_rss_mib"] for r in runs]),
        "max_temperature_c": stats([r["max_temperature_c"] for r in runs]),
        "metadata_lookup_seconds": stats([r["metadata_lookup_seconds"] for r in runs]),
        "unique_language_tags": sorted({tag for r in runs for tag in r["language_tag_review"]["unique_tags"]}),
        "runs_with_tags_in_final": sum(r["language_tag_review"]["final_contains_language_tags"] for r in runs),
        "runs_with_tags_in_stream": sum(r["language_tag_review"]["stream_contains_language_tags"] for r in runs),
    }
    for key in ("first_partial_seconds", "first_final_seconds", "first_nonempty_text_seconds", "first_chinese_character_seconds",
                "finish_to_drained_seconds", "total_seconds", "maximum_send_lateness_seconds"):
        summary[key] = stats([r["stream"][key] for r in runs])
    data = {"metadata": metadata, "runs": runs, "summary": summary}
    save(args.output / "results.json", data)
    report(args.output, data)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())