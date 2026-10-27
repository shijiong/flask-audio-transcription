"""Benchmark the unchanged FoundryBackend.summarize() using real saved text.

Each repetition uses a new process. Read-only sys.setprofile hooks observe
real SDK method calls and the production model context's yield/resume boundary;
no backend/SDK methods, prompts, generation settings, or results are replaced.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from importlib.metadata import version
import inspect
import json
import os
from pathlib import Path
import platform
import resource
import subprocess
import sys
import threading
import time
import traceback
from types import CodeType
from typing import Any

from benchmark_live_en import save, sha256, stats, telemetry, throttled

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "flask-audio-transcription/src/foundry_backend.py"
DEFAULT_SOURCE = ROOT / "flask-audio-transcription/data/5f1c9abaeace4d05a9079f087ef935e9/transcript.json"
ALIAS = "qwen3-0.6b"


def worker(args: argparse.Namespace) -> int:
    started = time.perf_counter()
    run: dict[str, Any] = {"run": args.run, "pid": os.getpid(), "status": "error",
                           "started_at": datetime.now(timezone.utc).isoformat(),
                           "throttling_before": throttled(), "progress": [], "profile_spans": []}
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
        from foundry_backend import FoundryBackend, SUMMARY_MODEL

        if SUMMARY_MODEL != ALIAS:
            raise RuntimeError(f"Production summary alias changed: {SUMMARY_MODEL}")
        text = (args.output / "input.txt").read_text(encoding="utf-8")
        if not text.strip():
            raise ValueError("No nonempty source text; no replacement input is generated")
        run["input_characters"] = len(text)
        run["input_sha256"] = sha256(args.output / "input.txt")
        backend = FoundryBackend()
        tick = time.perf_counter()
        manager = backend._initialize(lambda message: print(message, flush=True))
        run["preflight_sdk_initialization_seconds"] = time.perf_counter() - tick
        tick = time.perf_counter()
        candidate = manager.catalog.get_model(ALIAS)
        run["preflight_catalog_seconds"] = time.perf_counter() - tick
        if candidate is None:
            raise RuntimeError(f"{ALIAS} is absent from the actual catalog")
        info = candidate.info
        run["model"] = {
            "alias": ALIAS, "id": info.id, "file_size_mb": info.file_size_mb,
            "cached_before": candidate.is_cached,
            "execution_provider": info.runtime.execution_provider if info.runtime else None,
            "device": str(info.runtime.device_type) if info.runtime else None,
            "concrete_python_class": f"{type(candidate).__module__}.{type(candidate).__qualname__}",
        }
        # Code objects only: do not monkeypatch, call load separately, preload,
        # subclass, or bypass the model() that summarize() actually uses.
        targets = {getattr(type(candidate), name).__code__: name
                   for name in ("load", "unload", "download")}
        complete_code = next(c for c in FoundryBackend.summarize.__code__.co_consts
                             if isinstance(c, CodeType) and c.co_name == "complete")
        targets[complete_code] = "complete"
        model_code = FoundryBackend.model.__wrapped__.__code__
        run["instrumentation"] = {
            "method": "read-only sys.setprofile; no monkeypatch or replacement implementation",
            "model_generator_file": inspect.getfile(FoundryBackend),
            "sdk_methods": {name: {"file": code.co_filename, "first_line": code.co_firstlineno}
                            for code, name in targets.items()},
        }
        active: dict[int, dict[str, Any]] = {}
        bounds: dict[str, float] = {}

        def profile(frame: Any, event: str, arg: Any) -> None:
            code = frame.f_code
            if code is model_code:
                if event == "return" and arg is not None:
                    bounds["model_yield"] = time.perf_counter()
                    actual = frame.f_locals["model"]
                    run["model"]["actual_summarize_model_id"] = actual.info.id
                elif event == "call" and "model_yield" in bounds:
                    bounds["model_resume"] = time.perf_counter()
                return
            name = targets.get(code)
            if name is None or event not in ("call", "return"):
                return
            now = time.perf_counter()
            if event == "call":
                span: dict[str, Any] = {"method": name, "start_seconds": now}
                if name == "complete":
                    span["input_characters"] = len(frame.f_locals["content"])
                active[id(frame)] = span
            else:
                span = active.pop(id(frame), None)
                if span is not None:
                    span["end_seconds"] = now
                    span["duration_seconds"] = now - span["start_seconds"]
                    if name == "complete":
                        span["output_characters"] = len(arg) if isinstance(arg, str) else None
                    run["profile_spans"].append(span)

        def progress(message: str) -> None:
            run["progress"].append({"seconds": time.perf_counter() - started, "message": message})
            print(message, flush=True)

        if sys.getprofile() is not None:
            raise RuntimeError("An existing profiler is active; refusing to replace it")
        tick = time.perf_counter()
        try:
            sys.setprofile(profile)
            summary_text = backend.summarize(text, progress)
        finally:
            call_end = time.perf_counter()
            sys.setprofile(None)
            run["summarize_call_seconds_including_model_context"] = call_end - tick
        if not summary_text.strip():
            raise RuntimeError("Real summarize() returned an empty summary")
        loads = [s for s in run["profile_spans"] if s["method"] == "load"]
        unloads = [s for s in run["profile_spans"] if s["method"] == "unload"]
        completions = [s for s in run["profile_spans"] if s["method"] == "complete"]
        if len(loads) != 1 or len(unloads) != 1 or not completions:
            raise RuntimeError("Expected exactly one real load/unload and at least one production complete()")
        if not bounds.get("model_resume", 0) >= bounds.get("model_yield", float("inf")):
            raise RuntimeError("The production model yield/resume boundary was not observed")
        run.update({
            "model_load_seconds": loads[0]["duration_seconds"],
            "model_unload_seconds": unloads[0]["duration_seconds"],
            "model_download_or_cache_check_seconds": sum(s["duration_seconds"] for s in run["profile_spans"] if s["method"] == "download"),
            "summarize_pure_seconds": bounds["model_resume"] - bounds["model_yield"],
            "complete_total_seconds": sum(s["duration_seconds"] for s in completions),
            "complete_call_count": len(completions), "model_context_boundaries": bounds,
            "hierarchical_triggered": any(p["message"].startswith("Hierarchical summary, level ") for p in run["progress"]),
            "summary": summary_text, "summary_characters": len(summary_text),
        })
        run["model"]["cached_after"] = candidate.is_cached
        run["model"]["cache_path"] = candidate.get_path()
        run["status"] = "ok"
    except Exception as error:
        run["error"] = str(error)
        run["traceback"] = traceback.format_exc()
        traceback.print_exc()
    finally:
        try:
            if backend is not None and backend._manager is not None:
                backend._manager.close()
        except Exception:
            run["status"] = "error"
            run["cleanup_error"] = traceback.format_exc()
            traceback.print_exc()
        stop.set()
        watcher.join()
        samples.append(telemetry())
        run["telemetry_samples"] = samples
        run["peak_process_rss_mib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        temps = [s["temperature_c"] for s in samples if s["temperature_c"] is not None]
        run["max_temperature_c"] = max(temps) if temps else None
        swaps = [s["memory_mib"]["SwapTotal"] - s["memory_mib"]["SwapFree"] for s in samples]
        run["system_swap_used_mib_range"] = [min(swaps), max(swaps)]
        run["throttling_after"] = throttled()
        run["worker_elapsed_seconds_including_preflight_cleanup"] = time.perf_counter() - started
        save(args.output / f"run-{args.run:02d}.json", run)
    print(json.dumps({k: v for k, v in run.items() if k in {
        "run", "status", "model_load_seconds", "summarize_pure_seconds", "summarize_call_seconds_including_model_context",
        "hierarchical_triggered", "peak_process_rss_mib", "max_temperature_c", "error"}}, ensure_ascii=False), flush=True)
    return 0 if run["status"] == "ok" else 1


def formatted(value: dict[str, Any], unit: str = " s") -> str:
    if value["median"] is None:
        return "未测得"
    return f"{value['median']:.3f}{unit}（{value['min']:.3f}–{value['max']:.3f}）"


def report(output: Path, data: dict[str, Any]) -> None:
    m, s, runs = data["metadata"], data["summary"], data["runs"]
    first = runs[0]
    first_load = f"{s['first_observed_load_seconds']:.3f} s（本次首轮；{'已缓存' if first['model']['cached_before'] else '运行前未缓存'}）"
    throttle = "; ".join(f"run {r['run']}: {r['throttling_before']['raw']} → {r['throttling_after']['raw']}" for r in runs)
    rows = "\n".join(
        f"| {r['run']} / {r['pid']} | {r['model_load_seconds']:.3f} | {r['summarize_pure_seconds']:.3f} | "
        f"{r['summarize_call_seconds_including_model_context']:.3f} | {r['parent_process_wall_seconds']:.3f} | "
        f"{r['peak_process_rss_mib']:.1f} | {r['max_temperature_c']} | {r['hierarchical_triggered']} |"
        for r in runs)
    metrics = "\n".join(f"| {label} | {formatted(s[key])} |" for key, label in (
        ("model_load_seconds", "真实 SDK model.load()"),
        ("model_unload_seconds", "真实 SDK model.unload()"),
        ("model_download_or_cache_check_seconds", "model.download() / 缓存检查"),
        ("summarize_pure_seconds", "模型已就绪的摘要主体（纯耗时，不含 load/unload）"),
        ("complete_total_seconds", "生产 complete() 总耗时（纯耗时的子集）"),
        ("summarize_call_seconds_including_model_context", "完整 FoundryBackend.summarize()（含其 model 上下文）"),
        ("preflight_sdk_initialization_seconds", "单独的 benchmark SDK 初始化"),
        ("preflight_catalog_seconds", "单独的 benchmark catalog 元数据查询"),
        ("parent_process_wall_seconds", "fresh 子进程端到端 wall time（含 benchmark 前置/清理/结果写入）")))
    text = f"""# qwen3-0.6b 真实摘要 benchmark

日期：{m['started_at']}。一个原始文本 case，{len(runs)} 次独立进程，全部真实运行成功。

## 文章可用表格

摘要耗时明确 **不含模型加载和卸载**。时间为中位数（最小–最大），RSS/温度取所有轮次最大值。

| 模型 | 下载大小 | 首次加载 | 缓存后加载 | 摘要纯耗时 | 峰值 RSS | 输入字符 | 分层摘要 |
|---|---:|---|---|---|---:|---:|---|
| {ALIAS} | {first['model']['file_size_mb']} MB（catalog） | {first_load} | {formatted(s['cached_load_seconds'])} | {formatted(s['summarize_pure_seconds'])} | {s['peak_process_rss_mib']['max']:.1f} MiB | {m['input_characters']} | {'是' if any(r['hierarchical_triggered'] for r in runs) else '否，所有轮次均未触发'} |

- model id：`{first['model']['id']}`；execution provider：`{first['model']['execution_provider']}`。
- 最高采样温度：{s['max_temperature_c']['max']:.2f} °C。完整 throttle：{throttle}。
- 首次加载仅指本次第 1 轮，cached_before=true 时绝不能写成无缓存冷启动。没有删除用户缓存或 OS 页缓存；catalog 下载大小并非本次网络下载量。

## 素材与环境

- 指定来源：{m['source_path']} 的 `text` 字段。只使用原始转录，不使用旧 summary、TXT 标题、job 元信息；不重复、不裁剪、不清理语言标签。
- 字符数定义：Python `len(text)`，含空格、标点和语言标签，共 {m['input_characters']}；UTF-8 字节 {m['input_utf8_bytes']}。保存原样 input.txt 及来源/input 哈希。
- 本 case 小于生产的 3500 字符阈值；分层是否发生由真实 progress 中的 Hierarchical summary 消息判断，不通过合成长文本来伪造触发。
- {m['device']}；{m['platform']}；{m['cpu_count']} 核；Python {m['python']}；foundry-local-sdk {m['sdk_version']}。
- 生产 backend/service 以及 harness、SDK 方法源文件哈希保留；生产逻辑未修改。

## 计时边界：load / 纯摘要 / 端到端

| 指标 | 中位数（范围） |
|---|---|
{metrics}

1. **真实 model.load()**：只读 `sys.setprofile` 观察当前 concrete SDK load 方法进入/返回。实际方法由生产 FoundryBackend.model() 调用，包含 native Load；没有额外预加载、替换方法、独立假调用或重写摘要路径。
2. **summarize 纯耗时**：生产 model() generator 在成功加载后 yield 模型，到摘要完成后恢复 generator 的区间。含生产 complete()、prompt/JSON 构造、真实 ChatSession/Request 推理、响应解析/清理及 progress 回调，不含 catalog/download/load/unload。它不是 token 解码内核耗时。
3. **完整 summarize()**：直接调用真实 FoundryBackend.summarize(text, progress) 的进入到返回，含它内部全部 model 上下文（重复目录查询、缓存检查、load/unload）；本 harness 已先初始化 SDK 和查询元信息，所以它不是首次启动整个应用的时间。
4. **端到端子进程**：父进程 subprocess.run 的 wall time，含 Python/import、前置 SDK 初始化/catalog 查询、完整 summarize、manager.close、telemetry 和 JSON 写入；还含 benchmark 额外元数据观察开销，不冒充未经测量的 Flask/UI 端到端延迟。文本快照准备在父进程完成，不含此前 ASR。
5. 同步 profile 只观察调用，不 monkeypatch。该计时及 50 ms 采样有少量未扣除的测量开销。生产 temperature=0.2、max_tokens=700、enable_thinking=false 及英文摘要提示完全不变。

## 原始逐轮数据

| run / PID | load s | 纯摘要 s | summarize 完整 s | 子进程端到端 s | RSS MiB | °C | 分层 |
|---|---:|---:|---:|---:|---:|---:|---|
{rows}

## 输出与资源解释

- 每轮完整原始摘要单独保存；没有用历史 summary 替代生成，没有额外文本美化或手动修正。不同输出长度会影响耗时，字符数依次为 {[r['summary_characters'] for r in runs]}。
- 全进程峰值 RSS 来自 Linux ru_maxrss / 1024，包含 Python/SDK/native/模型/工作区，而非模型净内存。温度为每 50 ms 采样峰值。
- 系统 swap 使用范围（MiB）依次为 {[r['system_swap_used_mib_range'] for r in runs]}。这是全系统值，不能归因本进程，也不假设必须为 0。
- throttle 原文/位解码、load/complete 时间区间、实际 progress、温度/RSS/系统可用内存采样全部保留；历史 throttle 位不能直接归因本轮。桌面后台负载未清空，fresh process 不代表冷 OS 页缓存。
- 无人工参考摘要或质量分数；仅确认真实路径生成非空结果，不保证事实完整性或无幻觉。区域网络失败/回退原文在 run-XX.log，应结合单列的 catalog 耗时理解冷启动等待。

### Run 1 原始摘要

```text
{first['summary']}
```

## 复现

```bash
{sys.executable} benchmarks/benchmark_qwen_summary.py --source {m['source_path']} --repeats 3 --output benchmarks/results/qwen-summary-recheck
```

要求真实 Foundry SDK 与该输入文件；缺失或空 text 会失败，不生成替代素材。已有目录不覆盖。每轮全新 Python 子进程；失败日志/JSON保留，不编造缺失数据。
产物：REPORT.md、results.json、metadata.json、input.txt、每轮 run-XX.json/log/summary.txt。
"""
    (output / "REPORT.md").write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=ROOT / f"benchmarks/results/qwen-summary-{datetime.now().date().isoformat()}")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--run", type=int, default=1, help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.source, args.output = args.source.resolve(), args.output.resolve()
    if args.worker:
        return worker(args)
    if args.repeats < 3:
        parser.error("At least 3 fresh-process repetitions are required")
    if args.output.exists():
        parser.error("Output directory already exists; preserve measurements and use a fresh directory")
    if not args.source.is_file():
        parser.error("Source is missing; supply the actual transcript JSON path")
    source = json.loads(args.source.read_text(encoding="utf-8"))
    text = source.get("text")
    if not isinstance(text, str) or not text.strip():
        parser.error("No suitable nonempty text field; ask for an actual transcript path")
    args.output.mkdir(parents=True)
    (args.output / "input.txt").write_text(text, encoding="utf-8")
    device = Path("/proc/device-tree/model")
    metadata = {
        "started_at": datetime.now(timezone.utc).isoformat(), "case": "original_saved_transcript",
        "model_alias": ALIAS, "source_path": str(args.source), "source_field": "text",
        "source_sha256": sha256(args.source), "input_sha256": sha256(args.output / "input.txt"),
        "input_characters": len(text), "input_utf8_bytes": len(text.encode("utf-8")),
        "input_transformations": "none; original JSON text field, including whitespace and language tags",
        "device": device.read_text().rstrip("\0") if device.exists() else platform.machine(),
        "cpu_count": os.cpu_count(), "platform": platform.platform(), "python": sys.version.splitlines()[0],
        "sdk_version": version("foundry-local-sdk"), "backend_path": str(BACKEND),
        "backend_sha256": sha256(BACKEND), "service_sha256": sha256(BACKEND.parent / "service.py"),
        "harness_sha256": sha256(Path(__file__)),
        "shared_helpers_sha256": sha256(Path(__file__).with_name("benchmark_live_en.py")),
    }
    save(args.output / "metadata.json", metadata)
    runs = []
    for index in range(1, args.repeats + 1):
        print(f"=== Real {ALIAS} summary {index}/{args.repeats} ===", flush=True)
        command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--run", str(index), "--output", str(args.output)]
        tick = time.perf_counter()
        with (args.output / f"run-{index:02d}.log").open("w", encoding="utf-8") as log:
            try:
                exit_code = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=1800).returncode
            except subprocess.TimeoutExpired:
                exit_code = 124
        elapsed = time.perf_counter() - tick
        print((args.output / f"run-{index:02d}.log").read_text(), flush=True)
        path = args.output / f"run-{index:02d}.json"
        run = json.loads(path.read_text()) if path.exists() else {
            "run": index, "status": "error", "error": f"Worker exit {exit_code}; no JSON produced"}
        run.update({"exit_code": exit_code, "parent_process_wall_seconds": elapsed})
        if run["status"] == "ok":
            run["sdk_source_sha256"] = {p: sha256(Path(p)) for p in {
                v["file"] for k, v in run["instrumentation"]["sdk_methods"].items() if k != "complete"}}
        save(path, run)
        runs.append(run)
        save(args.output / "results.json", {"metadata": metadata, "runs": runs})
        if exit_code or run["status"] != "ok":
            (args.output / "REPORT.md").write_text(
                f"# qwen summary benchmark 未完成\n\n第 {index} 轮真实执行失败：{run.get('error', run.get('cleanup_error'))}\n\n原始日志与已完成结果保留；未编造缺失测量。\n", encoding="utf-8")
            return 1
        (args.output / f"run-{index:02d}-summary.txt").write_text(run["summary"], encoding="utf-8")
    summary = {key: stats([r[key] for r in runs]) for key in (
        "model_load_seconds", "model_unload_seconds", "model_download_or_cache_check_seconds",
        "summarize_pure_seconds", "complete_total_seconds", "summarize_call_seconds_including_model_context",
        "parent_process_wall_seconds", "preflight_sdk_initialization_seconds", "preflight_catalog_seconds",
        "peak_process_rss_mib", "max_temperature_c")}
    summary["first_observed_load_seconds"] = runs[0]["model_load_seconds"]
    summary["truly_uncached_first_load_measured"] = not runs[0]["model"]["cached_before"]
    summary["cached_load_seconds"] = stats([r["model_load_seconds"] for r in runs if r["model"]["cached_before"]])
    data = {"metadata": metadata, "runs": runs, "summary": summary}
    save(args.output / "results.json", data)
    report(args.output, data)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())