"""Local-only Flask application. Run directly; do not enable the debug reloader."""

from __future__ import annotations

import atexit
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
from typing import Any
from urllib.parse import urlsplit

from flask import Flask, jsonify, render_template, request, send_from_directory
from werkzeug.exceptions import HTTPException

from foundry_backend import FILE_MODELS, LIVE_MODELS, SUMMARY_MODEL, FoundryBackend
from service import ServiceError, TranscriptionService

ROOT = Path(__file__).resolve().parent.parent
EXTENSIONS = {".wav", ".mp3"}
LANGUAGES = {"auto", "zh-CN", "en", "de", "fr", "es", "ja", "ko"}


def create_app(config: dict[str, Any] | None = None, backend: Any = None) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.from_mapping(
        MAX_CONTENT_LENGTH=100 * 1024 * 1024,
        MAX_FORM_PARTS=32,
        DATA_DIR=os.environ.get("TRANSCRIPTION_DATA_DIR", str(ROOT / "data")),
        MAX_RECORDING_SECONDS=3600,
        IDLE_SECONDS=60,
        START_JANITOR=True,
        TRUSTED_HOSTS=["localhost", "127.0.0.1", "[::1]"],
    )
    if config:
        app.config.update(config)
    token = secrets.token_urlsafe(32)
    service = TranscriptionService(
        backend if backend is not None else FoundryBackend(), Path(app.config["DATA_DIR"]),
        app.config["MAX_RECORDING_SECONDS"], app.config["IDLE_SECONDS"], app.config["START_JANITOR"],
    )
    app.extensions["transcription_service"] = service
    atexit.register(service.close)

    @app.before_request
    def protect_local_api():
        if request.path.startswith("/api/") and request.method == "POST":
            origin = request.headers.get("Origin")
            if request.headers.get("Sec-Fetch-Site") == "cross-site" or (
                origin and urlsplit(origin).netloc != request.host
            ):
                raise ServiceError("Cross-site requests are not allowed.", 403)
            if not secrets.compare_digest(request.headers.get("X-App-Token", ""), token):
                raise ServiceError("Invalid request token. Refresh the page and try again.", 403)

    @app.after_request
    def security_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; media-src 'self' blob:; img-src 'self' data:; "
            "worker-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        return response

    @app.errorhandler(ServiceError)
    def service_error(error):
        return jsonify(error=str(error)), error.status

    @app.errorhandler(HTTPException)
    def http_error(error):
        message = "Request too large. Batch uploads must not exceed 100 MiB in total." if error.code == 413 else error.description
        return jsonify(error=message), error.code

    @app.get("/")
    def index():
        return render_template("index.html", token=token, live_models=LIVE_MODELS,
                               file_models=FILE_MODELS, summary_model=SUMMARY_MODEL)

    @app.get("/api/config")
    def configuration():
        # These are supported choices, not a promise of catalog availability on this hardware.
        return jsonify(live_models=LIVE_MODELS, file_models=FILE_MODELS,
                       summary_model=SUMMARY_MODEL, max_seconds=service.max_seconds)

    def validate_options(model: Any, language: Any, choices: tuple[str, ...]) -> tuple[str, str]:
        if not isinstance(model, str) or model not in choices:
            raise ServiceError("Select a supported transcription model.")
        if not isinstance(language, str) or language not in LANGUAGES:
            raise ServiceError("This language option is not supported.")
        if model == "nemotron-speech-streaming-en-0.6b" and language not in {"auto", "en"}:
            raise ServiceError("The selected Nemotron model supports English only. Choose the multilingual model for other languages.")
        if model == "nemotron-speech-streaming-en-0.6b":
            language = "en"
        return model, language

    @app.post("/api/live")
    def start_live():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise ServiceError("The request must be a JSON object.")
        model, language = validate_options(body.get("model"), body.get("language", "auto"), LIVE_MODELS)
        job = service.reserve("live", model, language)
        service.start_live(job)
        return jsonify(job.snapshot()), 202

    @app.post("/api/jobs/<job_id>/audio")
    def audio(job_id):
        if request.mimetype != "application/octet-stream":
            raise ServiceError("Audio must use the application/octet-stream content type.", 415)
        try:
            sequence = int(request.headers.get("X-Audio-Sequence", ""))
            if sequence < 0:
                raise ValueError
        except ValueError as exc:
            raise ServiceError("A valid audio chunk sequence number is required.") from exc
        if request.content_length is None or request.content_length > 32000:
            raise ServiceError("Audio chunk too large or Content-Length is missing.", 413)
        return jsonify(service.append(job_id, request.get_data(), sequence))

    @app.post("/api/jobs/<job_id>/stop")
    def stop(job_id):
        return jsonify(service.stop(job_id))

    @app.get("/api/jobs/<job_id>")
    def job_status(job_id):
        return jsonify(service.get(job_id).snapshot())

    @app.post("/api/files")
    def files():
        model, language = validate_options(request.form.get("model"), request.form.get("language", "auto"), FILE_MODELS)
        uploads = request.files.getlist("files")
        if not uploads or len(uploads) > 10:
            raise ServiceError("Select 1 to 10 audio files per batch.")
        for upload in uploads:
            if not upload.filename or Path(upload.filename).suffix.lower() not in EXTENSIONS:
                raise ServiceError("Unsupported file extension. Select WAV or MP3 files only.")
        job = service.reserve("files", model, language)
        try:
            paths = []
            for index, upload in enumerate(uploads):
                name = (upload.filename or "audio").replace("\\", "/").rsplit("/", 1)[-1][:200]
                path = job.directory / f"upload-{index}{Path(name).suffix.lower()}"
                upload.save(path)
                if path.stat().st_size == 0:
                    raise ServiceError("Empty audio files cannot be uploaded.")
                paths.append((name, path))
            service.start_files(job, paths)
            return jsonify(job.snapshot()), 202
        except Exception:
            shutil.rmtree(job.directory, ignore_errors=True)
            service.abandon(job)
            raise

    @app.get("/api/jobs/<job_id>/artifacts/<name>")
    def artifact(job_id, name):
        if not re.fullmatch(r"[0-9a-f]{32}", job_id) or name not in {"recording.wav", "transcript.json", "transcript.txt"}:
            raise ServiceError("File not found.", 404)
        directory = service.data_dir / job_id
        # Never expose an unfinished WAV header or a partially written transcript.
        if not service.artifact_ready(job_id):
            raise ServiceError("The file has not finished saving yet.", 409)
        return send_from_directory(directory, name, as_attachment=request.args.get("download") == "1",
                                   download_name=f"{job_id}-{name}")

    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app = create_app()
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), threaded=True, debug=False)