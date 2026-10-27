# 实时英语转录 benchmark：真实 Foundry Local live session

测试日期：2026-10-19T02:10:33.745406+00:00。真实音频、真实模型、每次 fresh process，共 3 次。

## 可直接回填文章的表格

下列缓存后时间为 3 次中位数，括号为观测最小值–最大值；内存和温度取 3 次最大值。

| 模型 | 下载大小 | 首次加载 | 缓存后加载 | 首字延迟（partial） | 确认延迟（first final） | 峰值内存 | 最高温度 | 是否降频/欠压 |
|---|---:|---|---|---|---|---:|---:|---|
| nemotron-speech-streaming-en-0.6b | 696 MB（catalog） | 6.185 s（本次首轮；已缓存） | 3.197 s（3.162–6.185） | 未观察到（0/3 次；不是 0 ms） | 1.501 s（1.500–5.113） | 936.8 MiB | 59.50 °C | 否；3 次运行前后均为 0x0 |

## 环境与输入

- 设备：Raspberry Pi 5 Model B Rev 1.1；4 核；Linux-6.12.47+rpt-rpi-2712-aarch64-with-glibc2.41。
- Python：3.14.8 | packaged by Anaconda, Inc. | (main, Oct  1 2026, 20:30:23) [GCC 14.3.0]；Foundry Local SDK：2.1.0。
- 实际 model id：nemotron-speech-streaming-en-0.6b-generic-cpu:3；执行提供器：CPUExecutionProvider。
- 输入：/home/pi/Documents/Recording.mp3；SHA-256：b1acc2f9d03c2753c3a246a8e210c57fb73b630303c590ce1459d3ce3151b8e0。
- 原始音频：24000 Hz、2 声道；容器时长 16.488000 s。
- ffmpeg 解码、下混和重采样为 16 kHz / mono / signed PCM16LE，实际送入 527616 字节、16.488000 s；转换耗时 0.318 s，不计入模型/流式计时。
- 转换过程使用 ffmpeg/libswresample，输出格式与应用兼容，但并非声称与浏览器 AudioWorklet 使用相同重采样算法。没有补尾部静音、没有裁剪或替换输入。
- 每块 3200 字节 = 100 ms，末块按实际剩余采样数发送；首块 t=0 入队，后续按绝对 100 ms 截止时间发送，音频全部播放时长届满后 finish()。
- 真实路径：FoundryBackend.model() → FoundryBackend.live_session() → NativeLiveSession.append()/finish()/events()/close()；转录拼接复用 service.apply_event()。
- 没有 mock，没有批量 Whisper API，没有浏览器/Flask HTTP，也没有摘要模型。

## 指标定义与汇总

| 指标 | 中位数（观测范围） |
|---|---|
| 已缓存模型加载 | 3.197 s（3.162–6.185） |
| 首个非空 partial | 未观察到（0/3 次；不是 0 ms） |
| 首个非空 confirmed/final（排除 aggregate） | 1.501 s（1.500–5.113） |
| 首个任意非空文本（不等同 partial） | 1.501 s（1.500–5.113） |
| finish 调用至流完全排空 | 0.673 s（0.549–0.780） |
| 第一块 append 至完整流结束 | 17.161 s（17.037–17.269） |
| 每轮最大送块迟到 | 0.009 s（0.006–0.013） |
| 峰值进程 RSS | 936.438 MiB（935.828–936.797） |
| 每轮最高采样温度 | 58.950 °C（58.400–59.500） |

## 每次运行

| run / PID | load s | partial s | final s | finish→drain s | total s | peak RSS MiB | max °C | partial/final/aggregate | throttle 前→后 |
|---|---:|---|---:|---:|---:|---:|---:|---|---|
| 1 / 19335 | 6.185 | 未观察到 | 5.113 | 0.780 | 17.269 | 935.8 | 58.40 | 0/84/1 | throttled=0x0 → throttled=0x0 |
| 2 / 19505 | 3.162 | 未观察到 | 1.501 | 0.549 | 17.037 | 936.4 | 58.95 | 0/84/1 | throttled=0x0 → throttled=0x0 |
| 3 / 19697 | 3.197 | 未观察到 | 1.500 | 0.673 | 17.161 | 936.8 | 59.50 | 0/84/1 | throttled=0x0 → throttled=0x0 |

## 重要边界

- 首次加载是本次 benchmark 首轮真实 load，不是凭空声称清空下载缓存后的冷启动。若 cached_before=true，真正无缓存首次下载/加载未测；脚本不会删除用户缓存或全局 drop_caches。
- 加载计时通过真实 model() 的 ‘Loading …’ progress 回调至 with 成功进入，近似 model.load()，未替换 SDK 方法；SDK 初始化、目录查找、下载/缓存检查在其外。回调打印有极小测量开销。
- file_size_mb 是模型目录元数据的下载大小声明，不是本次网络传输量，也不是内存占用；两者不能混用。
- partial 必须是非空、event.final=false 且 aggregate=false；first final 必须是非空、event.final=true 且 aggregate=false。遵从仓库 copy_segment() 的真实事件标记，不把 confirmed token 改称 partial。
- 如果 partial 为 null，表示该模型/配置/素材没有可观察的 partial 事件，不能补成首字延迟；另列 first_nonempty_text 便于说明真实可见首字。
- 起点为第一块 append 调用前；流结束点为 events() 完整迭代结束（包括 final_response 释放），不含 session.close/model unload、加载或预处理。finish→drain 从 finish() 调用前计算。
- RSS 为 Linux resource.getrusage(RUSAGE_SELF).ru_maxrss / 1024，包括 Python、native runtime、模型、工作区及应用保留的 PCM 缓冲，不是模型净内存。50 ms 采样同时保留 /proc RSS 与温度。
- get_throttled 前后保留完整原始字符串与当前/历史位解码；历史标志可能来自启动后更早事件。最高温度是 50 ms 采样最大值而非绝对硬件峰值。
- fresh process 不等于冷 OS 文件缓存；桌面/VS Code 后台负载未停止。短音频、3 次运行不能代表长会话稳态或准确率。

## 实测发现与 SDK 警告

- 三轮各发送 **165 块**：164 × 100 ms + 88 ms，全部 527616 PCM 字节仅送入一次；每轮均观察到 **0 个 partial、84 个应用 confirmed token 事件、1 个 aggregate 全文事件**。三轮转录文本一致。
- **first final 的准确含义是仓库路径的首个 confirmed/non-partial token，不是全文转录完成。** `copy_segment()` 使用 `int(item.kind) != 1` 生成 `SpeechEvent.final`；SDK 的 `PARTIAL=1`，`NONE=0` 和 `FINAL=2` 都会进入应用的 confirmed 分支。当前路径不向外保留 native kind，因此本测试不能进一步声称这些事件全是 native `FINAL=2`。未改动此生产映射。全文完成应看 aggregate/流结束，而非表中的 first final。
- 三轮目录查询分别为 **62.427、75.464、92.178 秒**。日志出现 `Region detection probe failed` 与 `RegionFallback ... unhealthy (transport failure)`，随后仍从真实 catalog 成功解析模型并完成推理。这些是实际环境开销，独立保留在每轮 `metadata_lookup_seconds`，不包含在 load 或 first-token 指标中；**不可用约 3 秒 load 声称应用从启动到可用只需约 3 秒**。
- 三轮均出现 `Failed to set Nemotron lang_id '0' for 'en' ... encoder graph has no lang_id input` 警告。脚本保留与英语应用路径一致的 `language="en"`，没有静默换参数或换模型；该英语专用模型仍成功输出英文。此警告不是本次测试抛出的失败异常，原文保留在各轮日志。
- 所有温度采样时的交换空间均未被使用；各轮前后 `get_throttled` 当前位和历史位均为 0。首轮首字较慢的原因未被隔离，不能直接归因为某一种初始化或缓存机制。
- 完整性验证通过：三个不同 PID、三个正常退出码、原 MP3/标准化 PCM/生产 backend/运行脚本的 SHA-256 与 metadata 一致、165 块的 100 ms 目标时间正确、送块字节数相等、结束后保留的 `_audio_items` 为 0。仅运行脚本参数检查和这些结果断言，没有额外模拟推理或修改生产实现。

## 复现

运行环境：/home/pi/Documents/python/foundry-local/.conda/bin/python

```bash
/home/pi/Documents/python/foundry-local/.conda/bin/python benchmarks/benchmark_live_en.py --audio /home/pi/Documents/Recording.mp3 --output benchmarks/results/live-en-recheck --repeats 3
```

脚本拒绝覆盖已有 results.json。请为复测指定新的结果目录。需要 ffmpeg、ffprobe、vcgencmd 和已安装的真实 Foundry Local SDK。输入不存在会立即失败，不会生成替代音频。
原始产物：results.json、run-01/02/03.json（含完整事件、送块时间、progress、telemetry）、每轮日志和 transcript，以及标准化 PCM。
