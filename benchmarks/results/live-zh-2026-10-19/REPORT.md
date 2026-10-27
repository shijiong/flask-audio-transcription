# 实时中文转录 benchmark：真实 Foundry Local SDK

日期：2026-10-19T02:36:03.745653+00:00；语言：`zh-CN`；3 次 fresh process，全部成功。

## 文章简表

时间为中位数（观测范围），内存/温度取三次最大值。首次加载单列本次第 1 轮，不虚构无缓存冷启动。

| 模型 / 语言 | 下载大小 | 首次加载 | 缓存后加载 | first partial | first final/confirmed | 峰值 RSS | 最高温度 | 降频/欠压 | 语言标签 |
|---|---:|---|---|---|---|---:|---:|---|---|
| nemotron-3.5-asr-streaming-0.6b / zh-CN | 756 MB（catalog） | 3.268 s（本次首轮；已缓存） | 3.583 s（3.268–3.857） | 未观察到（0/3 次；不是 0 ms） | 1.482 s（1.468–4.996） | 994.0 MiB | 59.50 °C | 否；前后均 0x0 | `<zh-CN>` |

## 环境、素材与真实路径

- 硬件：Raspberry Pi 5 Model B Rev 1.1，4 核；Linux-6.12.47+rpt-rpi-2712-aarch64-with-glibc2.41。
- Python 3.14.8 | packaged by Anaconda, Inc. | (main, Oct  1 2026, 20:30:23) [GCC 14.3.0]；Foundry Local SDK 2.1.0；CPUExecutionProvider。
- model id：`nemotron-3.5-asr-streaming-0.6b-generic-cpu:3`；alias：`nemotron-3.5-asr-streaming-0.6b`；`file_size_mb=756`。
- 输入：/home/pi/Documents/output_16k_mono.wav；SHA-256：f74905ba0953dfd9f5e287efeb9bcf1b4fc8f70243dcf1746a6d3ecf8a6c7cd4。
- 输入音频头：{"sample_rate": 16000, "channels": 1, "sample_width_bytes": 2, "compression": "NONE", "frames": 255977}。
- 处理：Python wave.readframes: extract exact original 16kHz mono PCM16LE, discard WAV container only; no resample, trim, gain, or added silence；511954 字节 PCM，15.998563 s；预处理耗时 0.001 s，未计入流式或 load。
- 使用 FoundryBackend.model() 的真实下载/加载/卸载，并使用 live_session() 创建 NativeLiveSession。append()/finish()/events()/close() 均为仓库原实现；拼接使用 service.apply_event()。没有 mock、batch API、浏览器、HTTP 或摘要。
- 每 3200 字节 = 100 ms，首块 t=0，按绝对截止时间实时发送，最后不足 100 ms 原样发送；到完整音频时长后 finish。不补尾部静音，不裁剪、不循环音频。
- 首次加载是本次首轮 load；cached_before=true 时，真正无缓存下载/冷加载未测。缓存后中位数包括所有 cached_before=true 的轮次，不清理用户缓存或 OS 页缓存。
- load 计时以真实 model() 的 Loading progress 回调为开始、with 成功进入为结束，近似 model.load()；初始化/目录查询/下载与缓存检查另记，不包含其中。

## 延迟与流式总耗时

| 指标 | 中位数（最小–最大） |
|---|---|
| 首个非空 partial | 未观察到（0/3 次；不是 0 ms） |
| 首个应用 confirmed（排除 aggregate） | 1.482 s（1.468–4.996） |
| 首个非空文本（可能只是语言标签） | 1.482 s（1.468–4.996） |
| 首个包含汉字的事件 | 1.482 s（1.468–4.996） |
| finish 至完全排空 | 0.559 s（0.546–2.066） |
| 第一块 append 至流结束 | 16.558 s（16.545–18.067） |
| 每轮最大送块迟到 | 0.008 s（0.003–0.013） |
| 每轮 RSS 峰值 | 993.094 MiB（993.016–993.984） |
| 每轮温度峰值 | 57.850 °C（57.300–59.500） |
| 目录查询（不含 load） | 23.249 s（13.456–62.648） |

## 原始逐轮结果

| run / PID | load s | partial s | confirmed s | 首汉字 s | drain s | total s | RSS MiB | °C | partial/confirmed/aggregate | throttle 前→后 |
|---|---:|---|---|---|---:|---:|---:|---:|---|---|
| 1 / 21817 | 3.268 | 未观察到 | 4.996 | 4.996 | 2.066 | 18.067 | 993.1 | 57.30 | 0/82/1 | throttled=0x0 → throttled=0x0 |
| 2 / 22017 | 3.583 | 未观察到 | 1.468 | 1.468 | 0.559 | 16.558 | 994.0 | 57.85 | 0/82/1 | throttled=0x0 → throttled=0x0 |
| 3 / 22132 | 3.857 | 未观察到 | 1.482 | 1.482 | 0.546 | 16.545 | 993.0 | 59.50 | 0/82/1 | throttled=0x0 → throttled=0x0 |

## 语言标签与中文结果解读

- 所有原始事件和拼接文本均未去标签、未改写。检测最终文本和完整 token 流（先拼接，避免漏检跨事件标签）；观察到：`<zh-CN>`。
- 最终文本带标签的轮次：3/3；token 流带标签的轮次：3/3。
- 各轮最终文本汉字数：[74, 74, 74]。包含汉字只证明有中文输出，不是识别准确率验证。
- 如果标签存在，建议文章明确提及：原生多语言输出可能夹带语言标记，不能当作用户口述内容；标签可能比真正的中文内容更早出现。表中的 first final 是原始事件口径，首个包含汉字的延迟另列，避免把标签延迟误称为中文首字。
- 语言标签名称不能单独作为识别语言或准确率的证据；即使出现 `<en-US>`，也要结合实际中文文本判断。本测试不在生产代码中过滤标签。
- 没有人工参考或直接听审，不能提供 CER/WER 或保证专有词正确。`auto` 为可选，本轮只实测主场景 zh-CN，不声称测过 auto。

### 原始输出示例（run 1，未清理）

```text
 全网最受欢迎普通话朗读六十篇完整合及重棒发布, 和光影八号一起学习普通话, 走遍天下都不怕, 视频由央视主持人康辉朗独。 <zh-CN> 同时也是普通话考试必备朗读视频, 您准备好了吗? <zh-CN>
```

## 事件及计量边界

- partial = 非空且 event.final=false、aggregate=false。confirmed = 非空且 event.final=true、aggregate=false。copy_segment() 将 native kind!=1 映射为 final=True；NONE=0 和 FINAL=2 均可进入应用 confirmed。因此 first final 不是 native FINAL=2 的保证，也不是全文完成。
- 第一块 append 调用前开始计时，events() 迭代和 final_response 释放全部结束为终点。finish→drain 包含 finish 调用，不含 close/unload。
- 所有原始语言标签算作非空输出，但另计首个含汉字事件。不观察到 partial 时 JSON 为 null，不替换为 confirmed，也不是 0 秒。
- RSS 为 ru_maxrss/1024 的全进程峰值，包含 Python/SDK/native/模型/工作区/保留音频，不是模型净内存。温度与 /proc RSS 每 50 ms 采样；温度为采样峰值。
- get_throttled 保存完整原文和位解码；历史位可来自此前系统事件，不应直接归因本轮。后台桌面负载未关，fresh process 不等于 OS 冷页缓存。
- 目录网络失败/区域回退或 SDK 警告原文在 run-XX.log；如果发生，查询耗时已单列，不能将约几秒 load 误写成应用完整启动时间。

## 本次结果的具体解读及核验

- 三次最终转录完全一致，均含 **74 个汉字、两个 `<zh-CN>`**，未观察到 `<en-US>`。每轮为 **0 个非空 partial、82 个非空应用 confirmed、1 个 aggregate**。这说明当前真实路径可持续产出中文 token，但不能将“没有 partial”改写成“没有实时输出”。
- 本样本的首个非空 confirmed 事件已经含汉字，首个包含汉字的事件延迟与 first confirmed 相同；不是以语言标签抢先到达来缩短表格延迟。首个汉字只是文本内容出现的指标，不等于通过听审确认的口述对应时刻。
- 原始文本可读、主题为普通话朗读视频介绍，但有“完整合及重棒发布”“朗独”等疑似需人工校对的字词。没有参考稿或听审，不判定其具体错误率，也不擅自将输出改成推测的正确词。
- **建议在文章中明确提及语言标签**：在本次 `zh-CN` 配置下，三轮均在原始中文输出中出现两个 `<zh-CN>`；它们应当作为原生输出的格式特征展示，而不当作口述内容。若产品界面另行过滤，应注明属于展示层清理，同时保留原始结果。不能据此推断未测的 `auto` 设置是否也会出现标签。
- 目录查询三次分别为 **62.648、23.249、13.456 秒**，日志实际包含区域探测失败和 `RegionFallback ... transport failure`。这些开销已独立记录，未计入模型 load 或从第一块音频开始的延迟。不能将缓存 load 的约 3.6 秒描述为应用完整启动时间。
- 系统交换空间的采样使用量：run 1 为 **0 MiB**；run 2 从 **0 增至 13 MiB**；run 3 为 **13 MiB**。同期最低系统可用内存分别约 **3698、3548、3420 MiB**。这些是全系统值，不能证明被交换页属于 benchmark，也不能声称全程无 swap；后台桌面负载仍存在。三次前后 `get_throttled` 均为 `0x0`，与 swap 是不同指标。
- 完整性已核验：3 个独立 PID、真实正常退出码、原 WAV 与提取 PCM 逐字节相同、生产 backend/service 及脚本哈希一致、每轮全部 **511954 字节**仅送入一次；**159 × 100 ms + 98.5625 ms** 共 160 块；完整排空后 `_audio_items` 为 0。
- 最小验证只运行入口参数检查与实测结果断言，没有额外模型 mock 或重复无关测试。验证中“全系统不使用 swap”的假设断言曾失败，进一步检查确认是上述真实环境数据；所有音频/进程/节奏/释放结构检查均通过，未删除或修改任何原始测量来满足该假设。

## 复现与文件

```bash
/home/pi/Documents/python/foundry-local/.conda/bin/python benchmarks/benchmark_live_zh.py --audio /home/pi/Documents/output_16k_mono.wav --language zh-CN --repeats 3 --output benchmarks/results/live-zh-recheck
```

需要真实 SDK、ffprobe，以及当前输入兼容 Python wave 的 PCM WAV。脚本拒绝覆盖已有 results.json；音频缺失/格式不兼容/模型不存在/SDK 失败会报错并保留失败，不生成替代输入或结果。
产物：metadata.json、results.json、REPORT.md、每轮 run-XX.json（全部事件/送块/telemetry/progress）、run-XX.log、run-XX-transcript.txt、从原 WAV 无损提取的 PCM。元数据记录输入、PCM、backend、service、脚本和复用计量工具的哈希。
