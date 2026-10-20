"""Exercise the frontend polling loop with controlled requests and timers."""

from pathlib import Path
import shutil
import subprocess

import pytest


def test_status_polling_interval_recovery_and_completion(tmp_path):
    chromium = shutil.which("chromium") or shutil.which("chromium-browser")
    if not chromium:
        pytest.skip("Chromium is required for frontend JavaScript regression tests")
    source = (Path(__file__).resolve().parents[1] / "src/static/app.js").read_text()
    harness = """
    // Stub only browser initialization; the real polling loop runs below.
    document.getElementById = () => ({addEventListener() {}});
    document.querySelector = () => ({content: 'test-token'});
    Object.defineProperty(navigator, 'mediaDevices', {value: undefined});
    Storage.prototype.getItem = () => null;
    function assert(ok, message) { if (!ok) throw new Error(message); }
    """
    checks = """
    (async () => {
      const timers = [], requests = [], rendered = [], errors = [];
      let releases = 0;
      window.setTimeout = (resolve, ms) => { timers.push({resolve, ms}); };
      api = path => new Promise((resolve, reject) => requests.push({path, resolve, reject}));
      render = job => rendered.push(job.status);
      releaseMicrophone = async () => { releases++; };
      setBusy = value => { busy = value; };
      showError = error => errors.push(error.message);
      const settle = async () => {
        // Flush promise continuations without real-time waits.
        for (let i = 0; i < 10; i++) await Promise.resolve();
      };
      jobId = 'test-job';
      pollGeneration = 1;
      busy = true;
      const running = poll(jobId, pollGeneration);
      assert(requests.length === 1, 'First status request must be immediate');
      assert(requests[0].path === '/api/jobs/test-job', 'Wrong status endpoint');
      await settle();
      assert(requests.length === 1 && timers.length === 0, 'Request overlapped while pending');

      requests[0].resolve({status: 'loading'});
      await settle();
      assert(rendered.join() === 'loading', 'Status was not rendered');
      assert(timers.length === 1 && timers[0].ms === 100, 'Successful poll must wait 100 ms');
      assert(requests.length === 1, 'Request started before the timer');
      timers.shift().resolve();
      await settle();
      assert(requests.length === 2 && timers.length === 0, 'Expected one pending request');

      requests[1].reject(new Error('temporary failure'));
      await settle();
      assert(errors.join() === 'temporary failure', 'Polling error was not reported');
      assert(timers.length === 1 && timers[0].ms === 300, 'Error retry must retain 300 ms pacing');
      timers.shift().resolve();
      await settle();
      requests[2].resolve({status: 'loading'});
      await settle();
      assert(timers.length === 1 && timers[0].ms === 100, 'Recovery must restore 100 ms polling');
      timers.shift().resolve();
      await settle();
      requests[3].resolve({status: 'done'});
      await running;
      assert(rendered.join() === 'loading,loading,done', 'Incorrect rendered statuses');
      assert(!busy && releases === 1, 'Completed job did not clean up');
      assert(timers.length === 0 && requests.length === 4, 'Polling continued after completion');
      document.body.textContent = 'POLL_TEST_OK';
    })().catch(error => { document.body.textContent = 'POLL_TEST_FAILED: ' + error.message; });
    """
    page = tmp_path / "poll-test.html"
    page.write_text(
        "<!doctype html><body><script>" + harness + source + checks + "</script>",
        encoding="utf-8",
    )
    result = subprocess.run(
        [chromium, "--headless", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
         f"--user-data-dir={tmp_path / 'browser'}", "--dump-dom", page.as_uri()],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "<body>POLL_TEST_OK</body>" in result.stdout, result.stdout