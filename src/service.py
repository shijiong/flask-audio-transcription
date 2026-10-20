"""Thread-safe jobs, ordered PCM ingestion, WAV recording and transcript storage."""

from __future__ import annotations

from array import array
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sys
import threading
import time
from typing import Any
import uuid
import wave

from foundry_backend import SAMPLE_RATE, SpeechEvent

LOGGER = logging.getLogger(__name__)
TERMINAL = {"done", "error"}


def now_iso() -> str:
    """Store wall-clock session boundaries in UTC, not per-speech offsets."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class ServiceError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Job:
    id: str
    kind: str
    model: str
    language: str
    directory: Path
    status: str = "loading"
    message: str = "Waiting for the model…"
    text: str = ""
    partial: str = ""
    summary: str = ""
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    started_at: str | None = None
    ended_at: str | None = None
    files: list[dict[str, Any]] = field(default_factory=list)
    bytes_received: int = 0
    audio_peak: int = 0
    next_sequence: int = 0
    stop_requested: bool = False
    finished: bool = False
    last_activity: float = field(default_factory=time.monotonic)
    lock: Any = field(default_factory=threading.RLock, repr=False)
    live: Any = field(default=None, repr=False)
    recording: Any = field(default=None, repr=False)
    worker: Any = field(default=None, repr=False)

    @property
    def duration(self) -> float:
        return self.bytes_received / (SAMPLE_RATE * 2)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "id": self.id, "kind": self.kind, "model": self.model,
                "status": self.status if self.finished or self.status not in TERMINAL else "saving",
                "message": self.message, "text": self.text,
                "partial": self.partial, "summary": self.summary, "error": self.error,
                "warnings": list(self.warnings), "duration": round(self.duration, 3),
                "audio_peak": self.audio_peak,
                "started_at": self.started_at, "ended_at": self.ended_at,
                "files": [dict(file) for file in self.files],
                "next_sequence": self.next_sequence,
                "recording_url": f"/api/jobs/{self.id}/artifacts/recording.wav"
                if (self.directory / "recording.wav").exists() and self.finished else None,
                "transcript_url": f"/api/jobs/{self.id}/artifacts/transcript.json"
                if (self.directory / "transcript.json").exists() and self.finished else None,
                "text_url": f"/api/jobs/{self.id}/artifacts/transcript.txt"
                if (self.directory / "transcript.txt").exists() and self.finished else None,
            }

    def update(self, message: str) -> None:
        with self.lock:
            self.message = message


def apply_event(job: Job, event: SpeechEvent) -> None:
    """Replace interim hypotheses; append final tokens; replace terminal full text."""
    with job.lock:
        if event.aggregate:
            job.text = event.text or job.text + job.partial
            job.partial = ""
            return
        if not event.final:
            job.partial = event.text
            return
        job.partial = ""
        # Nemotron emits subword tokens: preserve model spacing exactly.
        job.text += event.text


class TranscriptionService:
    def __init__(self, backend: Any, data_dir: Path, max_seconds: int = 3600,
                 idle_seconds: int = 60, start_janitor: bool = True) -> None:
        self.backend = backend
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.max_seconds = max_seconds
        self.idle_seconds = idle_seconds
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._busy = threading.Lock()
        self._shutdown = threading.Event()
        if start_janitor:
            threading.Thread(target=self._reaper, daemon=True, name="audio-idle-cleanup").start()

    def reserve(self, kind: str, model: str, language: str) -> Job:
        if not self._busy.acquire(blocking=False):
            raise ServiceError("A transcription or summary job is already running. Wait for it to finish before switching models.", 409)
        try:
            job_id = uuid.uuid4().hex
            directory = self.data_dir / job_id
            directory.mkdir(mode=0o700)
            job = Job(job_id, kind, model, language, directory)
            with self._lock:
                # Completed artifacts stay on disk, but keep only recent jobs in memory.
                if len(self.jobs) >= 50:
                    for old_id, old in list(self.jobs.items()):
                        if old.finished:
                            del self.jobs[old_id]
                            break
                self.jobs[job_id] = job
            return job
        except Exception:
            self._busy.release()
            raise

    def abandon(self, job: Job) -> None:
        with self._lock:
            self.jobs.pop(job.id, None)
        self._busy.release()

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self.jobs.get(job_id)
        if job is None:
            raise ServiceError("The job does not exist or has been removed from memory.", 404)
        return job

    def artifact_ready(self, job_id: str) -> bool:
        with self._lock:
            job = self.jobs.get(job_id)
        if job is None:
            return True  # Previously completed artifacts remain accessible after restart.
        with job.lock:
            return job.finished

    def start_live(self, job: Job) -> None:
        job.worker = threading.Thread(target=self._run_live, args=(job,), daemon=True)
        job.worker.start()

    def start_files(self, job: Job, paths: list[tuple[str, Path]]) -> None:
        job.files = [{"name": name, "status": "pending", "text": "", "error": ""} for name, _ in paths]
        job.worker = threading.Thread(target=self._run_files, args=(job, paths), daemon=True)
        job.worker.start()

    def append(self, job_id: str, pcm: bytes, sequence: int) -> dict[str, Any]:
        if not pcm or len(pcm) % 2 or len(pcm) > SAMPLE_RATE * 2:
            raise ServiceError("Audio chunks must contain non-empty PCM16LE data and be no longer than 1 second.")
        job = self.get(job_id)
        with job.lock:
            if job.kind != "live" or job.status != "recording" or job.stop_requested:
                raise ServiceError("This job is not currently recording.", 409)
            if sequence < job.next_sequence:
                # An HTTP response may be lost: retransmission must not duplicate the recording.
                return {"next_sequence": job.next_sequence}
            if sequence != job.next_sequence:
                raise ServiceError("Audio chunks are out of order. Stop and start a new recording.", 409)
            if job.bytes_received + len(pcm) > self.max_seconds * SAMPLE_RATE * 2:
                self._finish_locked(job)
                raise ServiceError(f"The {self.max_seconds}-second recording limit has been reached. Saving and generating a summary.", 409)
            # Save first, so any inference error does not erase the captured audio.
            job.recording.writeframesraw(pcm)
            samples = array("h", pcm)
            if sys.byteorder != "little":
                samples.byteswap()
            job.audio_peak = max(job.audio_peak, abs(min(samples)), abs(max(samples)))
            if job.started_at is None:
                job.started_at = now_iso()
            job.bytes_received += len(pcm)
            job.next_sequence += 1
            job.last_activity = time.monotonic()
            try:
                job.live.append(pcm)
            except Exception as exc:
                self._finish_locked(job)
                raise ServiceError(f"Failed to send audio to the model: {exc}", 503) from exc
            return {"next_sequence": job.next_sequence}

    def _finish_locked(self, job: Job) -> None:
        if job.stop_requested:
            return
        job.stop_requested = True
        job.ended_at = now_iso()
        if job.live is not None:
            job.status = "stopping"
            job.message = "Processing remaining audio and saving the recording…"
            try:
                job.live.finish()
            except Exception:
                LOGGER.exception("Failed to finish input queue")
                job.live.cancel()

    def stop(self, job_id: str) -> dict[str, Any]:
        job = self.get(job_id)
        with job.lock:
            if job.kind != "live":
                raise ServiceError("This is not a live transcription job.")
            if job.status in {"loading", "recording"}:
                self._finish_locked(job)
        return job.snapshot()

    def _run_live(self, job: Job) -> None:
        try:
            with self.backend.model(job.model, job.update) as model:
                with job.lock:
                    if job.stop_requested:
                        job.status = "stopping"
                    else:
                        job.live = self.backend.live_session(model, job.language)
                        job.recording = wave.open(str(job.directory / "recording.wav"), "wb")
                        job.recording.setnchannels(1)
                        job.recording.setsampwidth(2)
                        job.recording.setframerate(SAMPLE_RATE)
                        job.status = "recording"
                        job.message = "Model ready. Waiting for audio from the browser microphone."
                        job.last_activity = time.monotonic()
                if job.live is not None:
                    event_count = 0
                    try:
                        for event in job.live.events():
                            apply_event(job, event)
                            event_count += 1
                    finally:
                        with job.lock:
                            job.stop_requested = True
                            if job.ended_at is None:
                                job.ended_at = now_iso()
                            self._close_recording(job)
                            job.live.close()
                            job.live = None
                            LOGGER.info(
                                "Live job %s drained: chunks=%d bytes=%d audio_seconds=%.3f "
                                "events=%d text_chars=%d peak=%d",
                                job.id, job.next_sequence, job.bytes_received, job.duration,
                                event_count, len(job.text + job.partial), job.audio_peak,
                            )
            # Unload the ASR model before loading Qwen, to reduce memory use.
            with job.lock:
                if job.partial:
                    job.text += job.partial
                    job.partial = ""
                job.status = "summarizing"
                job.message = "Transcription complete. Generating key takeaways…"
            try:
                summary = self.backend.summarize(job.text, job.update)
                with job.lock:
                    job.summary = summary
            except Exception as exc:
                LOGGER.exception("Summary failed for %s", job.id)
                with job.lock:
                    job.warnings.append(f"Summary failed (transcript and recording preserved): {exc}")
            with job.lock:
                job.status = "done"
                job.message = "Transcription complete. Recording and text saved."
        except Exception as exc:
            self._fail(job, exc)
        finally:
            with job.lock:
                if job.live is not None:
                    try:
                        job.live.cancel()
                        job.live.close()
                    except Exception:
                        LOGGER.exception("Failed to close live session")
                    job.live = None
            self._complete(job)

    def _run_files(self, job: Job, paths: list[tuple[str, Path]]) -> None:
        try:
            with job.lock:
                job.started_at = now_iso()
            with self.backend.model(job.model, job.update) as model:
                with job.lock:
                    job.status = "transcribing"
                for index, (name, path) in enumerate(paths):
                    try:
                        with job.lock:
                            job.message = f"Transcribing {index + 1}/{len(paths)}: {name}"
                            job.files[index]["status"] = "transcribing"
                        text = self.backend.transcribe(model, path, job.language)
                        with job.lock:
                            job.files[index].update(status="done", text=text)
                    except Exception as exc:
                        LOGGER.exception("File transcription failed: %s", name)
                        with job.lock:
                            job.files[index].update(status="error", error=str(exc))
                    finally:
                        path.unlink(missing_ok=True)
            with job.lock:
                job.text = "\n\n".join(f"{file['name']}\n{file['text']}" for file in job.files if file["status"] == "done")
                failures = sum(file["status"] == "error" for file in job.files)
                job.status = "error" if failures == len(job.files) else "done"
                job.error = "All audio files failed to transcribe. Check the individual file errors." if job.status == "error" else ""
                if failures:
                    job.warnings.append(f"{failures} file(s) failed to transcribe; other results have been preserved.")
                job.message = "Batch transcription finished."
        except Exception as exc:
            self._fail(job, exc)
        finally:
            for _, path in paths:
                path.unlink(missing_ok=True)
            self._complete(job)

    @staticmethod
    def _close_recording(job: Job) -> None:
        if job.recording is not None:
            job.recording.close()
            job.recording = None
            if not job.bytes_received:
                job.warnings.append("No audio received from the browser microphone. Check the recording device and browser permissions, then start again.")
            elif not job.audio_peak:
                job.warnings.append("All recording samples are zero (complete silence), so speech cannot be recognized. Check the recording device, system input mute and volume. The recording has been preserved.")

    def _fail(self, job: Job, exc: Exception) -> None:
        LOGGER.exception("Job %s failed", job.id)
        with job.lock:
            job.status = "error"
            job.error = str(exc)
            job.message = "Job failed. Audio and text already received will still be saved."

    def _complete(self, job: Job) -> None:
        try:
            with job.lock:
                self._close_recording(job)
                if job.ended_at is None:
                    job.ended_at = now_iso()
                if job.partial:
                    job.text += job.partial
                    job.partial = ""
                payload = job.snapshot()
                payload["status"] = job.status
                (job.directory / "transcript.json").write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                lines = [f"Model: {job.model}", f"Start time: {job.started_at or 'Not started'}",
                         f"End time: {job.ended_at}", "", "Transcript:", job.text]
                lines.extend(["", "Summary:", job.summary, "", *job.warnings, job.error])
                (job.directory / "transcript.txt").write_text("\n".join(lines), encoding="utf-8")
        except Exception as exc:
            self._fail(job, exc)
        finally:
            with job.lock:
                self._busy.release()
                job.finished = True

    def reap_idle(self) -> None:
        with self._lock:
            jobs = list(self.jobs.values())
        for job in jobs:
            with job.lock:
                if job.status == "recording" and time.monotonic() - job.last_activity > self.idle_seconds:
                    job.warnings.append("The browser did not send audio before the idle timeout. Recording stopped automatically.")
                    self._finish_locked(job)

    def _reaper(self) -> None:
        while not self._shutdown.wait(5):
            self.reap_idle()

    def close(self) -> None:
        self._shutdown.set()
        with self._lock:
            jobs = list(self.jobs.values())
        for job in jobs:
            if job.kind == "live":
                self.stop(job.id)