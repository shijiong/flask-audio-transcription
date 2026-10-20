"""Contract tests use fake inference; no model downloads or microphone required."""

from contextlib import contextmanager
from datetime import datetime
import io
import json
import queue
import re
import struct
import threading
import time
import wave

import pytest

from app import create_app
from foundry_backend import LIVE_MODELS, SUMMARY_MODEL, FoundryBackend, SpeechEvent
from service import Job, ServiceError, apply_event


class FakeLive:
    def __init__(self):
        self.queue = queue.Queue()
        self.audio = bytearray()
        self.closed = False

    def append(self, pcm):
        self.audio.extend(pcm)
        self.queue.put(SpeechEvent("Hello", final=False))

    def finish(self):
        self.queue.put(SpeechEvent("Hello"))
        self.queue.put(SpeechEvent(" world."))
        self.queue.put(SpeechEvent("Hello world.", aggregate=True))
        self.queue.put(None)

    def events(self):
        while (event := self.queue.get()) is not None:
            yield event

    def cancel(self):
        self.queue.put(None)

    def close(self):
        self.closed = True


class FakeBackend:
    def __init__(self):
        self.live = None
        self.summary_error = False
        self.model_error = False
        self.models = []
        self.unloaded = []

    @contextmanager
    def model(self, alias, progress):
        if self.model_error:
            raise RuntimeError("model not available")
        self.models.append(alias)
        progress("Test model ready")
        try:
            yield object()
        finally:
            self.unloaded.append(alias)

    def live_session(self, model, language):
        self.live = FakeLive()
        return self.live

    def transcribe(self, model, path, language):
        if path.read_bytes() == b"bad":
            raise RuntimeError("invalid audio")
        return "文件转录文字"

    def summarize(self, text, progress):
        assert LIVE_MODELS[0] in self.unloaded
        if self.summary_error:
            raise RuntimeError("summary unavailable")
        return "- Key point: Hello world."


@pytest.fixture
def setup(tmp_path):
    backend = FakeBackend()
    app = create_app({"TESTING": True, "DATA_DIR": str(tmp_path), "START_JANITOR": False}, backend)
    client = app.test_client()
    page = client.get("/").get_data(as_text=True)
    token = re.search(r'name="app-token" content="([^"]+)"', page).group(1)
    headers = {"X-App-Token": token}
    yield client, app.extensions["transcription_service"], backend, headers, tmp_path
    app.extensions["transcription_service"].close()


def wait_status(client, job_id, statuses):
    # Event-based bounded waits, not assumptions about worker scheduling.
    deadline = time.monotonic() + 5
    pause = threading.Event()
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").get_json()
        if job["status"] in statuses:
            return job
        pause.wait(0.005)
    pytest.fail(f"job did not reach {statuses}: {job}")


def start_live(client, headers):
    response = client.post("/api/live", json={"model": LIVE_MODELS[0], "language": "auto"}, headers=headers)
    assert response.status_code == 202
    job_id = response.get_json()["id"]
    wait_status(client, job_id, {"recording", "error"})
    return job_id


def test_page_config_security_and_validation(setup):
    client, _, _, headers, _ = setup
    page = client.get("/").get_data(as_text=True)
    assert SUMMARY_MODEL in page
    assert 'id="started-at"' in page and 'id="ended-at"' in page
    assert 'id="microphone-device"' in page and 'id="microphone-level"' in page
    assert '<html lang="en">' in page
    assert "Live transcription" in page and "Audio files" in page
    assert "Auto-detect" in page and 'value="zh-CN">Chinese' in page
    assert 'accept=".wav,.mp3"' in page and "WAV / MP3 only" in page
    assert 'aria-describedby="file-audio-requirements"' in page
    assert "16 kHz · Mono" in page and "WAV: 16-bit PCM" in page
    assert "MP3: 16 kHz mono audio" in page and "not automatically converted" in page
    assert not any(name in page for name in ("M4A", "FLAC", "OGG", "WEBM", "AAC", "MP4"))
    assert not re.search(r"[\u3400-\u9fff]", page)
    assert "JSON timeline" not in page and "Click the timeline" not in page
    assert client.get("/api/config").get_json()["file_models"] == ["whisper-small", "whisper-tiny"]
    assert "frame-ancestors 'none'" in client.get("/").headers["Content-Security-Policy"]
    assert client.post("/api/live", json={"model": LIVE_MODELS[0]}).status_code == 403
    assert client.post("/api/live", json={"model": LIVE_MODELS[0]}, headers={**headers, "Origin": "https://evil.example"}).status_code == 403
    invalid_model = client.post("/api/live", json={"model": "qwen3-0.6b"}, headers=headers)
    assert invalid_model.status_code == 400
    assert invalid_model.get_json()["error"] == "Select a supported transcription model."
    assert client.post("/api/live", json=[], headers=headers).status_code == 400
    assert client.post("/api/live", json={"model": LIVE_MODELS[1], "language": "zh-CN"}, headers=headers).status_code == 400
    assert client.get("/api/jobs/no-such-job").status_code == 404
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400


def test_live_ordering_recording_summary_and_downloads(setup):
    client, service, backend, headers, root = setup
    job_id = start_live(client, headers)
    assert client.get(f"/api/jobs/{job_id}/artifacts/recording.wav").status_code == 409
    assert client.post("/api/live", json={"model": LIVE_MODELS[1]}, headers=headers).status_code == 409
    pcm = b"\x01\x00" * 1600
    audio_headers = {**headers, "Content-Type": "application/octet-stream", "X-Audio-Sequence": "0"}
    assert client.post(f"/api/jobs/{job_id}/audio", data=pcm, headers=audio_headers).status_code == 200
    assert client.post(f"/api/jobs/{job_id}/audio", data=pcm, headers=audio_headers).get_json()["next_sequence"] == 1
    assert client.post(f"/api/jobs/{job_id}/audio", data=pcm, headers={**audio_headers, "X-Audio-Sequence": "2"}).status_code == 409
    assert client.post(f"/api/jobs/{job_id}/audio", data=b"x", headers=audio_headers).status_code == 400
    assert client.post(f"/api/jobs/{job_id}/audio", data=pcm, headers={**headers, "Content-Type": "text/plain"}).status_code == 415
    assert client.post(f"/api/jobs/{job_id}/stop", headers=headers).status_code == 200
    job = wait_status(client, job_id, {"done"})
    service.get(job_id).worker.join(timeout=5)
    assert job["text"] == "Hello world."
    assert job["summary"].startswith("- Key point")
    assert job["duration"] == 0.1
    assert job["audio_peak"] == 1
    assert job["warnings"] == []
    assert "segments" not in job
    assert datetime.fromisoformat(job["started_at"]) <= datetime.fromisoformat(job["ended_at"])
    assert backend.live.audio == pcm
    assert backend.live.closed
    with wave.open(str(root / job_id / "recording.wav")) as recording:
        assert recording.getframerate() == 16000
        assert recording.getnchannels() == 1
        assert recording.getsampwidth() == 2
        assert recording.readframes(1600) == pcm
    for url in (job["recording_url"], job["transcript_url"], job["text_url"]):
        assert client.get(url).status_code == 200
        assert client.get(url + "?download=1").headers["Content-Disposition"].startswith("attachment")
    persisted = json.loads((root / job_id / "transcript.json").read_text())
    assert persisted["text"] == "Hello world."
    assert persisted["started_at"] == job["started_at"]
    assert persisted["ended_at"] == job["ended_at"]
    assert "segments" not in persisted and "timestamp_source" not in persisted
    saved_text = (root / job_id / "transcript.txt").read_text()
    assert "Start time:" in saved_text and "End time:" in saved_text
    assert "Model:" in saved_text and "Transcript:" in saved_text and "Summary:" in saved_text
    assert "Timeline:" not in saved_text and "timestamp_source" not in saved_text
    assert client.post(f"/api/jobs/{job_id}/stop", headers=headers).status_code == 200
    assert client.get(f"/api/jobs/{job_id}").get_json()["ended_at"] == job["ended_at"]
    assert client.post(f"/api/jobs/{job_id}/audio", data=pcm, headers=audio_headers).status_code == 409
    assert client.get(f"/api/jobs/{job_id}/artifacts/upload-0.wav").status_code == 404


def test_summary_failure_keeps_audio_and_text(setup):
    client, _, backend, headers, _ = setup
    backend.summary_error = True
    job_id = start_live(client, headers)
    client.post(f"/api/jobs/{job_id}/stop", headers=headers)
    job = wait_status(client, job_id, {"done"})
    assert any("Summary failed" in warning for warning in job["warnings"])
    assert job["recording_url"] and job["text_url"]
    assert job["text"] == "Hello world."


def test_missing_model_releases_busy_slot(setup):
    client, _, backend, headers, _ = setup
    backend.model_error = True
    job_id = start_live(client, headers)
    assert wait_status(client, job_id, {"error"})["error"] == "model not available"
    backend.model_error = False
    job_id = start_live(client, headers)
    client.post(f"/api/jobs/{job_id}/stop", headers=headers)
    wait_status(client, job_id, {"done"})


def test_batch_upload_isolates_failures_and_removes_sources(setup):
    client, _, _, headers, root = setup
    response = client.post("/api/files", data={"model": "whisper-tiny", "language": "auto", "files": [
        (io.BytesIO(b"audio"), "../../good.wav"), (io.BytesIO(b"bad"), "bad.mp3")
    ]}, headers=headers)
    assert response.status_code == 202
    job_id = response.get_json()["id"]
    job = wait_status(client, job_id, {"done"})
    assert job["files"][0]["name"] == "good.wav"
    assert job["files"][0]["text"] == "文件转录文字"
    assert job["files"][1]["status"] == "error"
    assert len(job["warnings"]) == 1
    assert datetime.fromisoformat(job["started_at"]) <= datetime.fromisoformat(job["ended_at"])
    assert "segments" not in job
    assert not list((root / job_id).glob("upload-*"))
    assert not (root.parent / "good.wav").exists()


@pytest.mark.parametrize("filename,content", [
    ("evil.exe", b"x"), ("empty.wav", b""),
    *((f"unsupported.{extension}", b"audio") for extension in ("m4a", "flac", "ogg", "webm", "aac", "mp4")),
])
def test_invalid_upload(setup, filename, content):
    client, service, _, headers, _ = setup
    response = client.post("/api/files", data={"model": "whisper-tiny", "files": (io.BytesIO(content), filename)}, headers=headers)
    assert response.status_code == 400
    if content:
        assert response.get_json()["error"] == "Unsupported file extension. Select WAV or MP3 files only."
    job = service.reserve("live", LIVE_MODELS[0], "auto")
    service.abandon(job)


@pytest.mark.parametrize("filename", ["recording.wav", "recording.mp3", "recording.WAV", "recording.MP3"])
def test_supported_upload_extensions(setup, filename):
    client, _, _, headers, _ = setup
    response = client.post("/api/files", data={
        "model": "whisper-tiny", "files": (io.BytesIO(b"audio"), filename),
    }, headers=headers)
    assert response.status_code == 202
    job = wait_status(client, response.get_json()["id"], {"done"})
    assert job["files"][0]["name"] == filename
    assert job["files"][0]["text"] == "文件转录文字"


def test_upload_limit_and_idle_stop(setup):
    client, service, _, headers, _ = setup
    client.application.config["MAX_CONTENT_LENGTH"] = 40
    assert client.post("/api/files", data=b"x" * 41, headers=headers).status_code == 413
    client.application.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024
    job_id = start_live(client, headers)
    job = service.get(job_id)
    with job.lock:
        job.last_activity -= service.idle_seconds + 1
    service.reap_idle()
    result = wait_status(client, job_id, {"done"})
    assert "idle timeout" in result["warnings"][0]


def test_recording_duration_limit(setup):
    client, service, _, headers, _ = setup
    service.max_seconds = 1
    job_id = start_live(client, headers)
    audio_headers = {**headers, "Content-Type": "application/octet-stream", "X-Audio-Sequence": "0"}
    assert client.post(f"/api/jobs/{job_id}/audio", data=b"\0\0" * 16000, headers=audio_headers).status_code == 200
    assert client.post(f"/api/jobs/{job_id}/audio", data=b"\0\0", headers={**audio_headers, "X-Audio-Sequence": "1"}).status_code == 409
    assert wait_status(client, job_id, {"done"})["duration"] == 1


def test_partial_replacement_token_concat_and_aggregate(tmp_path):
    job = Job("id", "live", LIVE_MODELS[0], "auto", tmp_path)
    job.bytes_received = 32000
    apply_event(job, SpeechEvent("Hel", final=False))
    apply_event(job, SpeechEvent("Hello", final=False))
    assert job.partial == "Hello" and job.text == ""
    apply_event(job, SpeechEvent("Hello"))
    apply_event(job, SpeechEvent(" world."))
    assert job.text == "Hello world."
    apply_event(job, SpeechEvent("Hello world!", aggregate=True))
    assert job.text == "Hello world!" and job.partial == ""
    assert "segments" not in job.snapshot()
    apply_event(job, SpeechEvent(" next", final=False))
    apply_event(job, SpeechEvent("", final=False))
    assert job.partial == ""
    apply_event(job, SpeechEvent(" next", final=False))
    apply_event(job, SpeechEvent("", aggregate=True))
    assert job.text == "Hello world! next" and job.partial == ""


def test_realtime_text_is_visible_before_recording_stops(setup):
    client, service, backend, headers, _ = setup
    job_id = start_live(client, headers)
    job = service.get(job_id)
    assert job.snapshot()["started_at"] is None
    audio_headers = {**headers, "Content-Type": "application/octet-stream", "X-Audio-Sequence": "0"}
    client.post(f"/api/jobs/{job_id}/audio", data=b"\0\0" * 1600, headers=audio_headers)
    # Queue order ensures the streamed events are consumed before this signal.
    consumed = threading.Event()
    original_apply = apply_event

    def track_event(current, event):
        original_apply(current, event)
        if event.text == " world":
            consumed.set()

    from unittest.mock import patch
    with patch("service.apply_event", track_event):
        backend.live.queue.put(SpeechEvent("Hel", final=False))
        backend.live.queue.put(SpeechEvent("Hello", final=False))
        backend.live.queue.put(SpeechEvent("Hello"))
        backend.live.queue.put(SpeechEvent(" wor", final=False))
        backend.live.queue.put(SpeechEvent(" world", final=False))
        assert consumed.wait(5)
    snapshot = client.get(f"/api/jobs/{job_id}").get_json()
    assert snapshot["status"] == "recording"
    assert snapshot["text"] == "Hello" and snapshot["partial"] == " world"
    assert snapshot["started_at"] and snapshot["ended_at"] is None
    client.post(f"/api/jobs/{job_id}/stop", headers=headers)
    assert wait_status(client, job_id, {"done"})["text"] == "Hello world."


def test_recording_end_time_precedes_summary(setup):
    client, service, backend, headers, _ = setup
    job_id = start_live(client, headers)
    job = service.get(job_id)
    audio_headers = {**headers, "Content-Type": "application/octet-stream", "X-Audio-Sequence": "0"}
    client.post(f"/api/jobs/{job_id}/audio", data=b"\x01\0" * 1600, headers=audio_headers)
    original_summary = backend.summarize

    def summarize(text, progress):
        assert job.ended_at is not None
        assert datetime.fromisoformat(job.started_at) <= datetime.fromisoformat(job.ended_at)
        return original_summary(text, progress)

    backend.summarize = summarize
    response = client.post(f"/api/jobs/{job_id}/stop", headers=headers).get_json()
    ended_at = response["ended_at"]
    assert ended_at is not None
    result = wait_status(client, job_id, {"done"})
    assert result["ended_at"] == ended_at
    assert result["warnings"] == [] and result["summary"]


def test_stopping_without_audio_does_not_invent_recording_start(setup):
    client, _, _, headers, _ = setup
    job_id = start_live(client, headers)
    client.post(f"/api/jobs/{job_id}/stop", headers=headers)
    result = wait_status(client, job_id, {"done"})
    assert result["started_at"] is None and result["ended_at"]
    assert result["text"] == "Hello world."  # Fake inference output, not captured audio.
    assert result["duration"] == 0
    assert "No audio received from the browser microphone" in result["warnings"][0]


def test_zero_pcm_warns_and_preserves_recording(setup):
    client, _, _, headers, root = setup
    job_id = start_live(client, headers)
    pcm = b"\0\0" * 1600
    audio_headers = {**headers, "Content-Type": "application/octet-stream", "X-Audio-Sequence": "0"}
    client.post(f"/api/jobs/{job_id}/audio", data=pcm, headers=audio_headers)
    client.post(f"/api/jobs/{job_id}/stop", headers=headers)
    result = wait_status(client, job_id, {"done"})
    assert result["audio_peak"] == 0
    assert any("complete silence" in warning for warning in result["warnings"])
    assert result["recording_url"]
    with wave.open(str(root / job_id / "recording.wav")) as recording:
        assert recording.readframes(1600) == pcm
    persisted = json.loads((root / job_id / "transcript.json").read_text())
    assert persisted["warnings"] == result["warnings"]


def test_audio_peak_handles_signed_pcm_and_initial_silence(setup):
    client, _, _, headers, _ = setup
    job_id = start_live(client, headers)
    audio_headers = {**headers, "Content-Type": "application/octet-stream", "X-Audio-Sequence": "0"}
    client.post(f"/api/jobs/{job_id}/audio", data=b"\0\0" * 1600, headers=audio_headers)
    client.post(f"/api/jobs/{job_id}/audio", data=struct.pack("<hhh", -32768, 32767, 0),
                headers={**audio_headers, "X-Audio-Sequence": "1"})
    client.post(f"/api/jobs/{job_id}/stop", headers=headers)
    result = wait_status(client, job_id, {"done"})
    assert result["audio_peak"] == 32768
    assert result["warnings"] == []


def test_failure_keeps_unconfirmed_text_and_end_time(setup):
    client, service, _, _, root = setup
    job = service.reserve("live", LIVE_MODELS[0], "auto")
    apply_event(job, SpeechEvent("确认文字"))
    apply_event(job, SpeechEvent("未确认文字", final=False))
    service._fail(job, RuntimeError("stream interrupted"))
    service._complete(job)
    snapshot = client.get(f"/api/jobs/{job.id}").get_json()
    assert snapshot["status"] == "error" and snapshot["ended_at"]
    assert snapshot["text"] == "确认文字未确认文字" and snapshot["partial"] == ""
    assert "segments" not in json.loads((root / job.id / "transcript.json").read_text())


@pytest.fixture
def summary_adapter(monkeypatch):
    import sys
    from types import SimpleNamespace

    state = SimpleNamespace(prompts=[], responses=[], closed=[], sessions=[])
    class TextItem:
        def __init__(self, text, kind):
            assert kind == "openai_json"
            self.text = text

    class Request:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            state.closed.append("request")
        def add_item(self, item):
            self.payload = json.loads(item.text)

    class Response:
        def __init__(self, payload):
            self.payload = payload
            self.closed = False
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.closed = True
            state.closed.append("response")
        def get_item(self, index):
            assert index == 0 and not self.closed
            return SimpleNamespace(text=json.dumps(self.payload))

    class ChatSession:
        def __init__(self, model):
            assert model.info.id == "qwen-test"
            state.sessions.append(self)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            state.closed.append("session")
        def process_request(self, request):
            state.prompts.append(request.payload)
            payload = state.responses.pop(0) if state.responses else {
                "choices": [{"message": {"content": "<think>internal</think>- Key point"}}],
            }
            return Response(payload)

    monkeypatch.setitem(sys.modules, "foundry_local_sdk.items", SimpleNamespace(
        TextItem=TextItem, TextItemType=SimpleNamespace(OPENAI_JSON="openai_json")))
    monkeypatch.setitem(sys.modules, "foundry_local_sdk.request", SimpleNamespace(Request=Request))
    monkeypatch.setitem(sys.modules, "foundry_local_sdk.session", SimpleNamespace(ChatSession=ChatSession))
    backend = FoundryBackend()
    @contextmanager
    def model(alias, progress):
        assert alias == SUMMARY_MODEL
        try:
            yield SimpleNamespace(info=SimpleNamespace(id="qwen-test"))
        finally:
            state.closed.append("model")
    backend.model = model
    state.backend = backend
    return state


def test_real_summary_adapter_splits_input_and_strips_thinking(summary_adapter):
    backend = summary_adapter.backend
    assert backend.summarize("文字" * 4000, lambda message: None) == "- Key point"
    prompts = summary_adapter.prompts
    assert len(prompts) == 4
    assert all(len(payload["messages"][1]["content"]) <= 3511 for payload in prompts)
    assert all("in English" in payload["messages"][0]["content"] for payload in prompts)
    assert all(payload["max_tokens"] == 700 and payload["temperature"] == 0.2 for payload in prompts)
    assert all(payload["chat_template_kwargs"] == {"enable_thinking": False} for payload in prompts)
    assert len(summary_adapter.sessions) == 4
    assert summary_adapter.closed == ["response", "request", "session"] * 4 + ["model"]


@pytest.mark.parametrize("message", [
    {"content": None, "reasoning_content": "Internal reasoning is not a summary."},
    {"content": ""},
    {"content": " \n "},
    {"content": "<think>Internal reasoning</think>"},
    {"content": "<think>Unfinished internal reasoning"},
])
def test_summary_rejects_empty_or_reasoning_only_responses(summary_adapter, message):
    summary_adapter.responses.append({"choices": [{"message": message}]})
    with pytest.raises(RuntimeError, match="summary model returned an empty response"):
        summary_adapter.backend.summarize("Meeting transcript.", lambda message: None)
    assert summary_adapter.closed == ["response", "request", "session", "model"]


def test_summary_handles_no_choices(summary_adapter):
    summary_adapter.responses.append({"choices": []})
    with pytest.raises(RuntimeError, match="summary model returned an empty response"):
        summary_adapter.backend.summarize("Meeting transcript.", lambda message: None)


def test_summary_empty_transcript_skips_inference(summary_adapter):
    assert summary_adapter.backend.summarize(" \n ", lambda message: None).startswith("No speech was recognized")
    assert summary_adapter.prompts == [] and summary_adapter.sessions == []