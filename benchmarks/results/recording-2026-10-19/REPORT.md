# Whisper benchmark: Raspberry Pi 5, original Recording MP3

Measured on 2026-10-19 UTC using the application's actual Foundry Local file-transcription API. All six inference runs succeeded; these are measured results, not model-size estimates.

## Test setup

- **Hardware:** Raspberry Pi 5 Model B Rev 1.1; four Cortex-A76 CPU cores, 2.4 GHz; 8 GB RAM.
- **Software:** Linux aarch64, Python 3.14.8, Foundry Local SDK 2.1.0, CPUExecutionProvider.
- **Input:** the original user-provided MP3, unchanged; **16.488 seconds**, **24,000 Hz**, **stereo**, 160 kb/s, 329,760 bytes. The original absolute path and SHA-256 are recorded in [metadata.json](metadata.json).
- **Variants:** `openai-whisper-tiny-generic-cpu:4` and `openai-whisper-small-generic-cpu:4`.
- **Settings:** language auto-detection; all other transcription settings unchanged from the app.
- **Repetitions:** three per model, one fresh Python/native process per run, no concurrent inference. Order: tiny, small, small, tiny, tiny, small.
- Both models were already downloaded. Download/cache checks took less than 1 ms per run; no model download was included in loading time.
- Fresh process/model loads **do not mean cold filesystem caches**. OS caches were not flushed, and other desktop/VS Code workloads remained running. This is a practical on-device comparison, not an otherwise-idle laboratory benchmark.

## Results

Time and RTF headline values are **medians of three runs**. Peak memory is the **maximum observed process high-water RSS** across those runs.

| Metric | whisper-tiny | whisper-small |
| --- | ---: | ---: |
| Model loading time, median | **1.875 s** | **2.810 s** |
| Model loading time, observed range | 1.781–2.634 s | 2.754–5.656 s |
| Transcription time, median | **2.676 s** | **10.398 s** |
| Transcription time, observed range | 2.421–3.175 s | 10.239–10.803 s |
| Real-Time Factor, median | **0.162** | **0.631** |
| Transcription speed relative to real time | **6.16×** | **1.59×** |
| Peak process memory, maximum RSS | **338.7 MiB** | **824.7 MiB** |
| SDK/catalog/cache/load to model-ready, median | 4.736 s | 8.499 s |
| SDK/catalog/cache/load + transcription, median | 7.165 s | 19.303 s |
| Text-only subjective quality | Understandable, with an awkward phrase | More natural wording and punctuation |

**RTF = transcription wall time / 16.488-second input duration.** RTF below 1 means the batch transcription completed faster than the recording's duration. It does not establish streaming latency or sustained performance on longer recordings. Load/startup time is excluded from RTF.

`model.load()` is measured independently of initialization, catalog lookup, cache/download checks, and transcription. Catalog lookup alone varied from 2.502 to 6.663 seconds; it must not be confused with model loading. Medians of separate stages need not sum to the median of a combined path.

Transcription timing includes the original MP3's runtime decoding/preprocessing, native audio-session creation, inference, and text retrieval through the same `FoundryBackend.transcribe()` method used by the app. HTTP upload, UI polling, artifact saving, summary generation, and model unload are not included. The combined startup-to-text measure also excludes Python interpreter startup and harness setup.

Memory uses Linux `resource.getrusage(RUSAGE_SELF).ru_maxrss`, converted from KiB to MiB. It includes Python, the SDK/native runtime, model allocations, and inference working memory; **it is not model-only RAM, total system RAM, or GPU memory**. Stage-level RSS sampling and thermal telemetry are also present in the raw JSON. No swap use was observed, and `vcgencmd get_throttled` reported `0x0` before and after every run. Maximum sampled CPU temperature across all stages was 59.5 °C.

### Individual runs

| Model | Run | Load (s) | Transcribe (s) | RTF | Peak process RSS (MiB) |
| --- | ---: | ---: | ---: | ---: | ---: |
| tiny | 1 | 2.634 | 3.175 | 0.193 | 337.5 |
| tiny | 2 | 1.875 | 2.676 | 0.162 | 338.4 |
| tiny | 3 | 1.781 | 2.421 | 0.147 | 338.7 |
| small | 1 | 5.656 | 10.803 | 0.655 | 824.5 |
| small | 2 | 2.754 | 10.239 | 0.621 | 824.3 |
| small | 3 | 2.810 | 10.398 | 0.631 | 824.7 |

The first observed load was slower for both models. With only three runs, the measurements do not isolate whether filesystem caching, runtime setup, or background scheduling caused that difference.

## Subjective quality: text review only

Each model produced identical text across its three runs. Both outputs communicate the same broad subject: sharing several links with fans, including concert videos, a photo gallery, and an album purchase.

- **tiny:** contains the awkward phrase **“You a band could give their fans”**, uses **“a couple new videos”**, and gives the list with little punctuation. It is understandable but benefits from editing.
- **small:** gives **“A band could give their fans a couple of new videos”** and adds commas later in the list. It reads more naturally, though **“behind the scenes photo gallery”** still has ambiguous punctuation.
- This is a **readability/coherence assessment only**. No verified reference transcript or direct listening-based review was available. The more fluent output is not automatically the more faithful transcription. WER, CER, word omissions, hallucinations, and audio-grounded accuracy cannot be established from these results alone. No numeric accuracy score is assigned.

Full outputs: [tiny transcript](whisper-tiny-run-1.txt) and [small transcript](whisper-small-run-1.txt).

## Practical conclusion

For this recording, **tiny transcribed 3.89× faster** and small used **2.43× as much peak process memory** (about 486 MiB more). Tiny is the better speed/RAM choice on this Pi. Small's extra cost bought noticeably smoother wording on this sample, so it may be preferable when editing effort matters more than latency. This short clip does not establish a general accuracy ranking.

**Input-format finding:** the native runtime accepted this original **24 kHz stereo MP3** without external conversion. The UI's 16 kHz mono note is preparation guidance, not a demonstrated hard runtime limit. The app does not externally convert uploads; any decoding/format handling in this test occurred inside the SDK/runtime. No UI or production inference settings were changed for this benchmark.

## Artifacts and reproduction

- [Reproducible benchmark harness](../../benchmark_whisper.py)
- [Full results and aggregate metrics](results.json)
- [Input, hardware, and methodology metadata](metadata.json)
- [Tiny first-run stage timings and telemetry](whisper-tiny-run-1.json)
- [Small first-run stage timings and telemetry](whisper-small-run-1.json)

The harness accepts the input audio path, an output directory, and `--repeats`; it defaults to both models and three repetitions. Per-run JSON, text, and native-output logs are retained next to this report. The source audio was neither modified nor copied into this results directory.