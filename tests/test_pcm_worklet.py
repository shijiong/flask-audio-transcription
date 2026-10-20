"""Exercise the real JavaScript resampler in Chromium, without a microphone/model."""

from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("sample_rate", [8000, 16000, 44100, 48000, 96000])
def test_pcm_resampling_flush_clipping_and_peak(tmp_path, sample_rate):
    chromium = shutil.which("chromium") or shutil.which("chromium-browser")
    if not chromium:
        pytest.skip("Chromium is required for AudioWorklet JavaScript regression tests")
    source = (Path(__file__).resolve().parents[1] / "src/static/pcm-worklet.js").read_text()
    harness = """
    let Processor;
    class AudioWorkletProcessor {
      constructor() { this.messages = []; this.port = {postMessage: data => this.messages.push(data)}; }
    }
    function registerProcessor(name, cls) { Processor = cls; }
    function assert(ok, message) { if (!ok) throw new Error(message); }
    """
    checks = """
    for (const value of [0, 0.25, -0.25, 2, -2]) {
      const recorder = new Processor();
      // Non-integral final duration exercises the last partial interval on flush.
      const frames = Math.floor(sampleRate * 0.237) + 1;
      for (let start = 0; start < frames; start += 128) {
        recorder.process([[new Float32Array(Math.min(128, frames - start)).fill(value)]]);
      }
      recorder.port.onmessage({data: 'flush'});
      const pcm = recorder.messages.filter(message => message.type === 'pcm');
      const sample = Math.round(Math.max(-1, Math.min(1, value)) * (value < 0 ? 32768 : 32767));
      let count = 0;
      for (const message of pcm) {
        const view = new DataView(message.buffer);
        assert(view.byteLength <= 3200, 'PCM chunk exceeds 100ms');
        assert(message.peak === Math.abs(sample), 'Incorrect signal level');
        for (let offset = 0; offset < view.byteLength; offset += 2) {
          assert(view.getInt16(offset, true) === sample, 'PCM lost or changed input signal');
          count++;
        }
      }
      assert(Math.abs(count - frames * 16000 / sampleRate) <= 1, 'Resampling duration drift');
      assert(recorder.messages.at(-1).type === 'flushed', 'Flush acknowledgement precedes audio');
      const messages = recorder.messages.length;
      recorder.process([[new Float32Array(128).fill(1)]]);
      assert(recorder.messages.length === messages, 'Audio emitted after flush');
    }
    document.body.textContent = 'PCM_TEST_OK';
    """
    page = tmp_path / "pcm-test.html"
    page.write_text(
        "<!doctype html><body><script>try {"
        + f"const sampleRate = {sample_rate};"
        + harness + source + checks
        + "} catch (error) { document.body.textContent = 'PCM_TEST_FAILED: ' + error.message; }</script>",
        encoding="utf-8",
    )
    result = subprocess.run(
        [chromium, "--headless", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
         f"--user-data-dir={tmp_path / 'browser'}", "--dump-dom", page.as_uri()],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "<body>PCM_TEST_OK</body>" in result.stdout, result.stdout