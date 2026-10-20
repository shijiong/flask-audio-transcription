"""Benchmark the app's real Whisper file API in fresh, sequential processes.

No models are run concurrently, no audio is converted, and downloads are timed
separately from load(). Results include native memory via Linux process RSS.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import resource
import runpy
import statistics
import subprocess
import sys
import threading
import time
import traceback
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[1]
MIB = 1024 * 1024


def save_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_number(path: str, divisor: float = 1) -> float | None:
    try:
        return float(Path(path).read_text().strip()) / divisor
    except (OSError, ValueError):
        return None


def command_output(*args: str) -> str | None:
    try:
        return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL, timeout=10).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def rss_bytes() -> int:
    resident_pages = int(Path("/proc/self/statm").read_text().split()[1])
    return resident_pages * os.sysconf("SC_PAGE_SIZE")


def telemetry() -> dict[str, Any]:
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        if key in {"MemAvailable", "SwapFree", "SwapTotal"}:
            memory[key] = int(value.split()[0]) * 1024
    return {
        "rss_bytes": rss_bytes(),
        "temperature_c": read_number("/sys/class/thermal/thermal_zone0/temp", 1000),
        "cpu_frequency_mhz": read_number("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq", 1000),
        "load_average": list(os.getloadavg()),
        "system_memory_bytes": memory,
    }


@contextmanager
def measured(result: dict[str, Any], name: str):
    stage: dict[str, Any] = {"before": telemetry()}
    samples = [stage["before"]]
    stop = threading.Event()

    def sample() -> None:
        while not stop.wait(0.05):
            samples.append(telemetry())

    watcher = threading.Thread(target=sample, daemon=True)
    watcher.start()
    print(f"START {name}", flush=True)
    cpu_start = time.process_time()
    start = time.perf_counter()
    try:
        yield
    finally:
        stage["seconds"] = time.perf_counter() - start
        stage["cpu_seconds"] = time.process_time() - cpu_start
        stop.set()
        watcher.join()
        stage["after"] = telemetry()
        samples.append(stage["after"])
        stage["sampled_peak_rss_mib"] = max(item["rss_bytes"] for item in samples) / MIB
        temperatures = [item["temperature_c"] for item in samples if item["temperature_c"] is not None]
        stage["max_temperature_c"] = max(temperatures) if temperatures else None
        stage["process_high_water_rss_mib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        stage["minimum_system_available_mib"] = min(item["system_memory_bytes"]["MemAvailable"] for item in samples) / MIB
        result["stages"][name] = stage
        print(f"END {name}: {stage['seconds']:.3f}s; process peak {stage['process_high_water_rss_mib']:.1f} MiB", flush=True)


def worker(args: argparse.Namespace) -> int:
    prefix = args.output / f"{args.model}-run-{args.run}"
    result: dict[str, Any] = {
        "model_alias": args.model, "run": args.run, "pid": os.getpid(),
        "audio_path": str(args.audio), "language": "auto", "stages": {},
        "started_at": datetime.now(timezone.utc).isoformat(),
        "throttling_before": command_output("vcgencmd", "get_throttled"),
    }
    backend = None
    model = None
    try:
        backend_class = runpy.run_path(str(APP_ROOT / "src/foundry_backend.py"))["FoundryBackend"]
        backend = backend_class()
        with measured(result, "sdk_initialization"):
            manager = backend._initialize(lambda message: print(message, flush=True))
        with measured(result, "catalog_lookup"):
            model = manager.catalog.get_model(args.model)
            if model is None:
                raise RuntimeError(f"{args.model} is unavailable on this platform")
            info = model.info
            result["model"] = {
                "id": info.id, "alias": info.alias, "version": info.version,
                "device": str(info.runtime.device_type) if info.runtime else None,
                "execution_provider": info.runtime.execution_provider if info.runtime else None,
                "file_size_mb": info.file_size_mb, "cached_before": model.is_cached,
            }
        with measured(result, "download_or_cache_check"):
            last_percent = -10

            def progress(percent: float) -> None:
                nonlocal last_percent
                if percent - last_percent >= 10:
                    print(f"Download {args.model}: {percent:.1f}%", flush=True)
                    last_percent = percent

            model.download(progress)
        result["model"]["cache_path"] = model.get_path()
        with measured(result, "model_load"):
            model.load()
        with measured(result, "transcription"):
            text = backend.transcribe(model, args.audio, "auto")
        prefix.with_suffix(".txt").write_text(text + "\n", encoding="utf-8")
        result["transcript"] = text
        result["transcript_characters"] = len(text)
        if not text.strip():
            raise RuntimeError("Model returned an empty transcript")
        result["status"] = "ok"
    except Exception as error:
        result["status"] = "error"
        result["error"] = str(error)
        traceback.print_exc()
    finally:
        result["peak_process_rss_mib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        if model is not None:
            try:
                with measured(result, "unload"):
                    model.unload()
            except Exception as error:
                result["unload_error"] = str(error)
        if backend is not None and backend._manager is not None:
            backend._manager.close()
        result["peak_process_rss_mib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        result["throttling_after"] = command_output("vcgencmd", "get_throttled")
        save_json(prefix.with_suffix(".json"), result)
    return 0 if result["status"] == "ok" else 1


def benchmark(args: argparse.Namespace) -> int:
    args.output.mkdir(parents=True, exist_ok=True)
    probe = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "a:0", "-show_streams", "-show_format",
        "-of", "json", str(args.audio),
    ], text=True))
    duration = float(probe["format"]["duration"])
    device = Path("/proc/device-tree/model")
    metadata = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "audio_path": str(args.audio), "sha256": hashlib.file_digest(args.audio.open("rb"), "sha256").hexdigest(),
        "audio_duration_seconds": duration, "audio_probe": probe,
        "device": device.read_text().rstrip("\0") if device.exists() else platform.machine(),
        "platform": platform.platform(), "python": sys.version, "sdk_version": version("foundry-local-sdk"),
        "cpu_count": os.cpu_count(), "initial_telemetry": telemetry(),
        "methodology": {
            "processes": "Fresh process per model/repetition; sequential, alternating model order",
            "load": "model.load() only; SDK/catalog/cache/download timings reported separately",
            "transcription": "Unmodified app FoundryBackend.transcribe(model, original_mp3, 'auto'); includes decoding/session creation",
            "rtf": "transcription wall seconds / ffprobe container duration seconds",
            "memory": "Linux ru_maxrss whole-process high-water RSS including native allocations; NOT model-only RAM or system memory",
            "cache": "Fresh model process, not an OS filesystem cold-cache benchmark; caches are not flushed",
            "quality": "Text-only qualitative comparison; no human reference or listening-based accuracy measurement",
        },
    }
    save_json(args.output / "metadata.json", metadata)
    runs = []
    failed = False
    for repeat in range(1, args.repeats + 1):
        models = args.models if repeat % 2 else list(reversed(args.models))
        for alias in models:
            prefix = args.output / f"{alias}-run-{repeat}"
            print(f"\n=== {alias}, repetition {repeat}/{args.repeats} ===", flush=True)
            command = [sys.executable, str(Path(__file__).resolve()), str(args.audio),
                       "--output", str(args.output), "--worker", "--model", alias, "--run", str(repeat)]
            with prefix.with_suffix(".log").open("w", encoding="utf-8") as log:
                completed = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=1800)
            print(prefix.with_suffix(".log").read_text(), flush=True)
            if not prefix.with_suffix(".json").exists():
                failed = True
                print(f"Worker failed before producing JSON, exit {completed.returncode}", flush=True)
                continue
            result = json.loads(prefix.with_suffix(".json").read_text())
            if result["status"] == "ok":
                result["rtf"] = result["stages"]["transcription"]["seconds"] / duration
                save_json(prefix.with_suffix(".json"), result)
            else:
                failed = True
            runs.append(result)
            save_json(args.output / "results.json", {"metadata": metadata, "runs": runs})
    summary = {}
    for alias in args.models:
        good = [run for run in runs if run["model_alias"] == alias and run["status"] == "ok"]
        if good:
            summary[alias] = {
                "runs": len(good),
                "median_load_seconds": statistics.median(run["stages"]["model_load"]["seconds"] for run in good),
                "median_transcription_seconds": statistics.median(run["stages"]["transcription"]["seconds"] for run in good),
                "median_rtf": statistics.median(run["rtf"] for run in good),
                "maximum_peak_process_rss_mib": max(run["peak_process_rss_mib"] for run in good),
            }
    save_json(args.output / "results.json", {"metadata": metadata, "runs": runs, "summary": summary})
    print("\nSUMMARY\n" + json.dumps(summary, indent=2), flush=True)
    return int(failed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--models", nargs="+", choices=["whisper-tiny", "whisper-small"], default=["whisper-tiny", "whisper-small"])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--model", choices=["whisper-tiny", "whisper-small"], help=argparse.SUPPRESS)
    parser.add_argument("--run", type=int, default=1, help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.audio = args.audio.resolve(strict=True)
    args.output = args.output.resolve()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    return worker(args) if args.worker else benchmark(args)


if __name__ == "__main__":
    raise SystemExit(main())