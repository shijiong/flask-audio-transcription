# qwen3-0.6b 真实摘要 benchmark

日期：2026-10-19T03:42:04.020708+00:00。一个原始文本 case，3 次独立进程，全部真实运行成功。

## 文章可用表格

摘要耗时明确 **不含模型加载和卸载**。时间为中位数（最小–最大），RSS/温度取所有轮次最大值。

| 模型 | 下载大小 | 首次加载 | 缓存后加载 | 摘要纯耗时 | 峰值 RSS | 输入字符 | 分层摘要 |
|---|---:|---|---|---|---:|---:|---|
| qwen3-0.6b | 593 MB（catalog） | 12.519 s（本次首轮；已缓存） | 12.085 s（10.006–12.519） | 14.052 s（13.488–16.568） | 1333.5 MiB | 786 | 否，所有轮次均未触发 |

- model id：`qwen3-0.6b-generic-cpu:4`；execution provider：`CPUExecutionProvider`。
- 最高采样温度：60.05 °C。完整 throttle：run 1: throttled=0x0 → throttled=0x0; run 2: throttled=0x0 → throttled=0x0; run 3: throttled=0x0 → throttled=0x0。
- 首次加载仅指本次第 1 轮，cached_before=true 时绝不能写成无缓存冷启动。没有删除用户缓存或 OS 页缓存；catalog 下载大小并非本次网络下载量。

## 素材与环境

- 指定来源：/home/pi/Documents/python/foundry-local/flask-audio-transcription/data/5f1c9abaeace4d05a9079f087ef935e9/transcript.json 的 `text` 字段。只使用原始转录，不使用旧 summary、TXT 标题、job 元信息；不重复、不裁剪、不清理语言标签。
- 字符数定义：Python `len(text)`，含空格、标点和语言标签，共 786；UTF-8 字节 786。保存原样 input.txt 及来源/input 哈希。
- 本 case 小于生产的 3500 字符阈值；分层是否发生由真实 progress 中的 Hierarchical summary 消息判断，不通过合成长文本来伪造触发。
- Raspberry Pi 5 Model B Rev 1.1；Linux-6.12.47+rpt-rpi-2712-aarch64-with-glibc2.41；4 核；Python 3.14.8 | packaged by Anaconda, Inc. | (main, Oct  1 2026, 20:30:23) [GCC 14.3.0]；foundry-local-sdk 2.1.0。
- 生产 backend/service 以及 harness、SDK 方法源文件哈希保留；生产逻辑未修改。

## 计时边界：load / 纯摘要 / 端到端

| 指标 | 中位数（范围） |
|---|---|
| 真实 SDK model.load() | 12.085 s（10.006–12.519） |
| 真实 SDK model.unload() | 0.269 s（0.256–0.291） |
| model.download() / 缓存检查 | 0.001 s（0.000–0.001） |
| 模型已就绪的摘要主体（纯耗时，不含 load/unload） | 14.052 s（13.488–16.568） |
| 生产 complete() 总耗时（纯耗时的子集） | 14.052 s（13.487–16.567） |
| 完整 FoundryBackend.summarize()（含其 model 上下文） | 26.842 s（23.751–28.945） |
| 单独的 benchmark SDK 初始化 | 0.137 s（0.119–0.166） |
| 单独的 benchmark catalog 元数据查询 | 62.864 s（62.763–63.508） |
| fresh 子进程端到端 wall time（含 benchmark 前置/清理/结果写入） | 90.140 s（87.790–92.496） |

1. **真实 model.load()**：只读 `sys.setprofile` 观察当前 concrete SDK load 方法进入/返回。实际方法由生产 FoundryBackend.model() 调用，包含 native Load；没有额外预加载、替换方法、独立假调用或重写摘要路径。
2. **summarize 纯耗时**：生产 model() generator 在成功加载后 yield 模型，到摘要完成后恢复 generator 的区间。含生产 complete()、prompt/JSON 构造、真实 ChatSession/Request 推理、响应解析/清理及 progress 回调，不含 catalog/download/load/unload。它不是 token 解码内核耗时。
3. **完整 summarize()**：直接调用真实 FoundryBackend.summarize(text, progress) 的进入到返回，含它内部全部 model 上下文（重复目录查询、缓存检查、load/unload）；本 harness 已先初始化 SDK 和查询元信息，所以它不是首次启动整个应用的时间。
4. **端到端子进程**：父进程 subprocess.run 的 wall time，含 Python/import、前置 SDK 初始化/catalog 查询、完整 summarize、manager.close、telemetry 和 JSON 写入；还含 benchmark 额外元数据观察开销，不冒充未经测量的 Flask/UI 端到端延迟。文本快照准备在父进程完成，不含此前 ASR。
5. 同步 profile 只观察调用，不 monkeypatch。该计时及 50 ms 采样有少量未扣除的测量开销。生产 temperature=0.2、max_tokens=700、enable_thinking=false 及英文摘要提示完全不变。

## 原始逐轮数据

| run / PID | load s | 纯摘要 s | summarize 完整 s | 子进程端到端 s | RSS MiB | °C | 分层 |
|---|---:|---:|---:|---:|---:|---:|---|
| 1 / 27843 | 12.519 | 14.052 | 26.842 | 90.140 | 1333.3 | 58.4 | False |
| 2 / 28016 | 12.085 | 16.568 | 28.945 | 92.496 | 1333.5 | 59.5 | False |
| 3 / 28189 | 10.006 | 13.488 | 23.751 | 87.790 | 1313.7 | 60.05 | False |

## 输出与资源解释

- 每轮完整原始摘要单独保存；没有用历史 summary 替代生成，没有额外文本美化或手动修正。不同输出长度会影响耗时，字符数依次为 [532, 716, 427]。
- 全进程峰值 RSS 来自 Linux ru_maxrss / 1024，包含 Python/SDK/native/模型/工作区，而非模型净内存。温度为每 50 ms 采样峰值。
- 系统 swap 使用范围（MiB）依次为 [[13.0, 32.0], [32.0, 132.0], [132.0, 164.0]]。这是全系统值，不能归因本进程，也不假设必须为 0。
- throttle 原文/位解码、load/complete 时间区间、实际 progress、温度/RSS/系统可用内存采样全部保留；历史 throttle 位不能直接归因本轮。桌面后台负载未清空，fresh process 不代表冷 OS 页缓存。
- 无人工参考摘要或质量分数；仅确认真实路径生成非空结果，不保证事实完整性或无幻觉。区域网络失败/回退原文在 run-XX.log，应结合单列的 catalog 耗时理解冷启动等待。

## 本次实测解读与完整性核验

- 三次模型在运行前均已缓存，实际 variant 均为 `qwen3-0.6b-generic-cpu:4`。下载进度显示 100% 是实际缓存检查的回调，不是重新传输了 593 MB。第一轮 12.519 秒是首次观测的真实 load，不是无缓存首次安装。
- 三轮日志均实际出现 `Region detection probe failed (status 0); defaulting to 'centralus'`，前置 catalog 查询范围 **62.763–63.508 秒**。端到端明显大于摘要调用的主要额外等待在这里；不能将 benchmark 的 90.140 秒端到端指标当作纯本地模型推理时间，也不能把约 12 秒 load 当作整个 fresh process 的启动时间。
- 三次输出均为英文 bullet points；输出分别 **532、716、427 个字符**，在未固定随机种子、保持生产 `temperature=0.2` 的条件下并不完全一致。第 1、2 轮偏逐句摘录，第 3 轮有主题标题；部分版本遗漏 Dale、礼物或字典等原文细节。输出原样保存，未据此宣称质量达标或给出自动质量分数。
- 实际每轮只有 **1 次生产 complete()**，progress 均为 `Generating the final summary…`，没有分层消息。这只是该 **786 字符**样本的结果，不代表已经验证长文分层性能。没有为触发分层而重复素材或生成虚构会议。
- 验证已通过：3 个独立 PID（27843、28016、28189）和成功退出码；原 JSON `text` 与 input.txt 一致；输入、来源、生产 backend/service、harness、复用计量工具及 SDK 方法源文件哈希一致；实际摘要模型 ID 与 catalog ID 一致；真实 `load → model yield → complete → model resume → unload` 时序成立；纯摘要不包含 load/unload；保存文本与原始返回值一致；汇总中位数可由逐轮数据重算。
- 最小验证为入口参数检查、语法/编辑器诊断及真实结果断言；没有修改生产逻辑，也没有运行 mock 模型来替代这三轮测量。

### Run 1 原始摘要

```text
- It was a wonderful trip, and we had an interesting vacation there.

- We arrived in New York by plane on the morning of July twenty third.

- It was sunny and hot first we rode to the mountains.

- I really enjoyed the clean air in the mountains in the afternoon.

- We visited the history museum because there were many visitors there we had to stay there for a long time.

- The museum was so big that it was dark when we got outside as for dinner we didn't have much money so we only ate a bowl of noodles but it was delicious.
```

## 复现

```bash
/home/pi/Documents/python/foundry-local/.conda/bin/python benchmarks/benchmark_qwen_summary.py --source /home/pi/Documents/python/foundry-local/flask-audio-transcription/data/5f1c9abaeace4d05a9079f087ef935e9/transcript.json --repeats 3 --output benchmarks/results/qwen-summary-recheck
```

要求真实 Foundry SDK 与该输入文件；缺失或空 text 会失败，不生成替代素材。已有目录不覆盖。每轮全新 Python 子进程；失败日志/JSON保留，不编造缺失数据。
产物：REPORT.md、results.json、metadata.json、input.txt、每轮 run-XX.json/log/summary.txt。
