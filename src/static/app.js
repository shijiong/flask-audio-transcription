"use strict";
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="app-token"]').content;
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const POLL_INTERVAL_MS = 100;
let jobId = null, busy = false, stopping = false, media = null, context = null, recorder = null;
let uploadQueue = [], uploadPromise = null, sequence = 0, lastJob = null, capturing = false;
let initializingAudio = false, flushResolve = null, renderKey = "", pollGeneration = 0;
let audioWatchdog = null, lastPCMAt = 0, lastSignalAt = 0, microphoneLabel = "Microphone";
const terminal = job => ["done", "error"].includes(job.status);

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.method === "POST") headers.set("X-App-Token", token);
  const response = await fetch(path, {...options, headers});
  let data;
  try { data = await response.json(); } catch { throw new Error(`Invalid server response (${response.status})`); }
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}
const jsonPost = (path, body = {}) => api(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
function showError(error) { $("error-message").textContent = error.message || String(error); $("error-message").hidden = false; }
function setBusy(value) {
  busy = value;
  for (const id of ["start-button", "upload-button", "microphone-device", "live-model", "live-language", "file-model", "file-language", "audio-files"]) $(id).disabled = value;
  $("status-dot").classList.toggle("busy", value);
}
async function refreshMicrophones() {
  if (!navigator.mediaDevices?.enumerateDevices) return;
  try {
    const selected = $("microphone-device").value;
    const devices = (await navigator.mediaDevices.enumerateDevices()).filter(device => device.kind === "audioinput");
    const options = [new Option("System default microphone", "")];
    devices.forEach((device, index) => {
      if (device.deviceId) options.push(new Option(device.label || `Microphone ${index + 1}`, device.deviceId));
    });
    $("microphone-device").replaceChildren(...options);
    if (options.some(option => option.value === selected)) $("microphone-device").value = selected;
  } catch { /* Device labels may be unavailable before permission is granted. */ }
}
function microphoneStatus(message, warning = false) {
  $("microphone-status").textContent = message;
  $("microphone-status").classList.toggle("capture-warning", warning);
}
function receivePCM(data) {
  lastPCMAt = performance.now();
  const peak = data.peak / 32768;
  $("microphone-level").value = Math.min(1, Math.sqrt(peak));
  if (data.peak > 1) {
    lastSignalAt = lastPCMAt;
    microphoneStatus(`${microphoneLabel}: receiving audio.`);
  }
  enqueue(data.buffer);
}
function checkMicrophone() {
  if (!capturing || stopping) return;
  const now = performance.now();
  if (now - lastPCMAt > 5000) {
    showError(new Error("Microphone capture stalled: no audio chunks received. Keep this page active, check microphone permissions, and start again."));
    void stopRecording();
  } else if (now - lastSignalAt > 5000) {
    microphoneStatus(`${microphoneLabel}: no sound detected. Check the recording device, system input mute and volume, or select another microphone and start again.`, true);
  }
}
function timeLabel(value) {
  const minutes = Math.floor(value / 60), seconds = Math.floor(value % 60);
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}
function dateLabel(value) {
  return value ? new Date(value).toLocaleString("en-US", {hour12: false}) : "—";
}
function displayedText(job) {
  return (job.text || "") + (job.kind === "live" ? job.partial || "" : "");
}
function textElement(tag, text, className) {
  const element = document.createElement(tag);
  element.textContent = text;
  if (className) element.className = className;
  return element;
}
function render(job) {
  lastJob = job;
  $("status-text").textContent = job.message;
  $("clock").textContent = timeLabel(job.duration);
  $("record-dot").classList.toggle("active", capturing && job.status === "recording");
  $("started-at").textContent = dateLabel(job.started_at);
  $("ended-at").textContent = job.ended_at ? dateLabel(job.ended_at) : (job.started_at ? "In progress" : "—");
  $("copy-button").disabled = !displayedText(job);
  $("warnings").hidden = !job.warnings.length;
  $("warnings").textContent = job.warnings.join("\n");
  if (job.error) showError(new Error(job.error));
  $("summary").textContent = job.summary || (job.status === "summarizing" ? "Summarizing topics, key points, and action items…" : "After live transcription ends, get an English summary of topics, key points, decisions, and action items.");
  $("summary").classList.toggle("muted", !job.summary);
  const key = JSON.stringify([job.kind, job.files, job.text, job.partial]);
  if (key !== renderKey) {
    renderKey = key;
    const container = $("transcript");
    const atBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 60;
    container.replaceChildren();
    if (job.kind === "files") {
      for (const file of job.files) {
        const row = textElement("section", "", "file-result");
        row.append(textElement("h3", file.name), textElement("p", ({pending: "Waiting to transcribe", transcribing: "Transcribing…", done: "Completed", error: "Transcription failed"})[file.status], "file-status"));
        row.append(textElement("p", file.error || file.text, "file-text"));
        container.append(row);
      }
    } else if (job.text || job.partial) {
      const row = textElement("p", "", "transcript-text");
      row.append(textElement("span", job.text || ""), textElement("span", job.partial || "", "partial"));
      container.append(row);
    }
    if (atBottom) container.scrollTop = container.scrollHeight;
  }
  $("empty-state").hidden = !!(job.text || job.partial || job.files.length);
  $("recording-section").hidden = !job.recording_url;
  if (job.recording_url && $("recording-player").getAttribute("src") !== job.recording_url) $("recording-player").src = job.recording_url;
  $("download-links").hidden = !job.text_url;
  $("recording-download").hidden = !job.recording_url;
  for (const [id, url] of [["recording-download", job.recording_url], ["text-download", job.text_url], ["json-download", job.transcript_url]]) {
    if (url) $(id).href = url + "?download=1";
  }
  $("stop-button").disabled = stopping || job.kind !== "live" || !["loading", "recording"].includes(job.status);
}
async function releaseMicrophone() {
  capturing = false;
  clearInterval(audioWatchdog);
  audioWatchdog = null;
  $("microphone-level").value = 0;
  $("record-dot").classList.remove("active");
  if (media) { media.getTracks().forEach(track => track.stop()); media = null; }
  recorder?.disconnect();
  recorder = null;
  const oldContext = context;
  context = null;
  if (oldContext && oldContext.state !== "closed") await oldContext.close();
}
async function drainUploads() {
  while (uploadQueue.length) {
    const buffer = uploadQueue[0];
    let result;
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        result = await api(`/api/jobs/${jobId}/audio`, {method: "POST", headers: {"Content-Type": "application/octet-stream", "X-Audio-Sequence": String(sequence)}, body: buffer});
        break;
      } catch (error) {
        if (attempt === 2) throw error;
        await delay(200 * (attempt + 1));
      }
    }
    sequence = result.next_sequence;
    uploadQueue.shift();
  }
}
function enqueue(buffer) {
  uploadQueue.push(buffer);
  if (uploadQueue.length > 100) {
    showError(new Error("Audio uploads are more than 10 seconds behind. Capture has stopped to prevent an unbounded backlog."));
    void stopRecording();
  }
  if (!uploadPromise) {
    uploadPromise = drainUploads().catch(error => {
      showError(error);
      uploadQueue = [];
      void stopRecording();
    }).finally(() => { uploadPromise = null; });
  }
}
async function beginAudio() {
  if (initializingAudio || capturing || stopping || !media) return;
  initializingAudio = true;
  try {
    const audioContext = context;
    await audioContext.audioWorklet.addModule("/static/pcm-worklet.js");
    if (stopping || !media || audioContext.state === "closed") return;
    recorder = new AudioWorkletNode(audioContext, "pcm-recorder");
    recorder.port.onmessage = ({data}) => {
      if (data.type === "pcm") receivePCM(data);
      if (data.type === "flushed" && flushResolve) { flushResolve(); flushResolve = null; }
    };
    recorder.onprocessorerror = () => {
      showError(new Error("The audio capture processor failed. Stop the session and start again."));
      void stopRecording();
    };
    const source = audioContext.createMediaStreamSource(media);
    const silence = audioContext.createGain();
    silence.gain.value = 0;
    source.connect(recorder).connect(silence).connect(audioContext.destination);
    await audioContext.resume();
    if (stopping) return;
    if (audioContext.state !== "running") throw new Error("The browser did not start audio capture. Click Start to try again.");
    capturing = true;
    lastPCMAt = lastSignalAt = performance.now();
    audioWatchdog = setInterval(checkMicrophone, 1000);
    microphoneStatus(`${microphoneLabel}: capture started. Speak and check that the input level changes.`);
    $("record-dot").classList.add("active");
    for (const track of media.getAudioTracks()) {
      track.addEventListener("mute", () => microphoneStatus(`${microphoneLabel}: input is temporarily unavailable. Check the device connection and system mute settings.`, true));
      track.addEventListener("ended", () => {
        if (capturing && !stopping) {
          showError(new Error("The microphone disconnected. Saving the audio already received."));
          void stopRecording();
        }
      });
    }
  } catch (error) { showError(error); void stopRecording(); }
  finally { initializingAudio = false; }
}
async function poll(id, generation) {
  let failures = 0;
  while (generation === pollGeneration && id === jobId) {
    try {
      const job = await api(`/api/jobs/${id}`);
      failures = 0;
      render(job);
      if (job.status === "recording" && !stopping) await beginAudio();
      if (!["loading", "recording"].includes(job.status) && media) await releaseMicrophone();
      if (terminal(job)) {
        await releaseMicrophone();
        setBusy(false);
        $("stop-button").disabled = true;
        return;
      }
    } catch (error) {
      failures++;
      showError(error);
      if (failures >= 5) {
        await releaseMicrophone();
        try { await jsonPost(`/api/jobs/${id}/stop`); } catch { /* server idle cleanup is the fallback */ }
        setBusy(false);
        $("stop-button").disabled = true;
        return;
      }
    }
    // Keep transient-error retries at their original pace.
    await delay(failures ? 300 : POLL_INTERVAL_MS);
  }
}
function beginJob(job) {
  jobId = job.id;
  stopping = false;
  sequence = job.next_sequence;
  uploadQueue = [];
  renderKey = "";
  try { localStorage.setItem("voice-notes-job", job.id); } catch { /* storage can be disabled */ }
  $("error-message").hidden = true;
  $("recording-player").removeAttribute("src");
  $("recording-player").load();
  render(job);
  void poll(job.id, ++pollGeneration);
}
async function startRecording() {
  if (busy) return;
  setBusy(true);
  stopping = false;
  $("error-message").hidden = true;
  try {
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) throw new Error("Microphone access requires a secure context. Use localhost, 127.0.0.1, or HTTPS.");
    // Unlock Web Audio in the click handler, not in polling after a long model download.
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    context = new AudioContextClass();
    if (!context.audioWorklet) throw new Error("Your browser does not support AudioWorklet. Use a recent version of Chrome, Edge, or Firefox.");
    const audioReady = context.resume();
    $("status-text").textContent = "Please allow microphone access in your browser…";
    const deviceId = $("microphone-device").value;
    // ASR needs the original signal, not browser communication DSP.
    const audio = {channelCount: {ideal: 1}, echoCancellation: false, noiseSuppression: false, autoGainControl: false};
    if (deviceId) audio.deviceId = {exact: deviceId};
    media = await navigator.mediaDevices.getUserMedia({audio, video: false});
    await audioReady;
    const track = media.getAudioTracks()[0];
    if (!track || track.readyState !== "live" || !track.enabled) throw new Error("No microphone audio track is available. Check your recording device.");
    microphoneLabel = track.label || "Selected microphone";
    await refreshMicrophones();
    microphoneStatus(`${microphoneLabel}: capture will start when the model is ready.`);
    const job = await jsonPost("/api/live", {model: $("live-model").value, language: $("live-language").value});
    beginJob(job);
  } catch (error) {
    await releaseMicrophone();
    setBusy(false);
    showError(error);
    $("status-text").textContent = "Could not start live transcription.";
  }
}
async function stopRecording() {
  if (stopping || !jobId) return;
  stopping = true;
  $("stop-button").disabled = true;
  try {
    if (recorder && context?.state === "running") {
      const flushed = new Promise(resolve => { flushResolve = resolve; });
      recorder.port.postMessage("flush");
      await Promise.race([flushed, delay(1500)]);
      flushResolve = null;
    }
    await releaseMicrophone();
    if (uploadPromise) await uploadPromise;
    render(await jsonPost(`/api/jobs/${jobId}/stop`));
  } catch (error) {
    showError(error);
    await releaseMicrophone();
    stopping = false;
    $("stop-button").disabled = false;
  }
}
let selectedFiles = [];
function selectFiles(files) {
  if (busy) return;
  const candidates = Array.from(files);
  if (candidates.some(file => !/\.(wav|mp3)$/i.test(file.name))) {
    selectedFiles = [];
    $("audio-files").value = "";
    $("selected-files").textContent = "No files selected";
    showError(new Error("Unsupported file type. Select WAV or MP3 files only."));
    return;
  }
  selectedFiles = candidates;
  $("error-message").hidden = true;
  $("selected-files").textContent = selectedFiles.length ? selectedFiles.map(file => file.name).join(", ") : "No files selected";
}
async function uploadFiles() {
  if (busy) return;
  if (!selectedFiles.length || selectedFiles.length > 10) return showError(new Error("Select 1 to 10 audio files."));
  if (selectedFiles.reduce((sum, file) => sum + file.size, 0) > 100 * 1024 * 1024) return showError(new Error("The total file size must not exceed 100 MiB."));
  setBusy(true);
  $("status-text").textContent = "Uploading audio…";
  try {
    const form = new FormData();
    form.append("model", $("file-model").value);
    form.append("language", $("file-language").value);
    selectedFiles.forEach(file => form.append("files", file));
    beginJob(await api("/api/files", {method: "POST", body: form}));
  } catch (error) { setBusy(false); showError(error); $("status-text").textContent = "Upload did not complete."; }
}
function switchTab(mode) {
  for (const name of ["live", "files"]) {
    $(name + "-panel").hidden = name !== mode;
    $(name + "-tab").classList.toggle("active", name === mode);
    $(name + "-tab").setAttribute("aria-selected", String(name === mode));
  }
}
$("live-tab").addEventListener("click", () => switchTab("live"));
$("files-tab").addEventListener("click", () => switchTab("files"));
for (const id of ["live-tab", "files-tab"]) $(id).addEventListener("keydown", event => {
  if (["ArrowLeft", "ArrowRight"].includes(event.key)) {
    event.preventDefault();
    const mode = id === "live-tab" ? "files" : "live";
    switchTab(mode); $(mode + "-tab").focus();
  }
});
$("start-button").addEventListener("click", startRecording);
$("stop-button").addEventListener("click", stopRecording);
$("upload-button").addEventListener("click", uploadFiles);
$("audio-files").addEventListener("change", event => selectFiles(event.target.files));
$("drop-zone").addEventListener("dragover", event => { event.preventDefault(); if (!busy) $("drop-zone").classList.add("dragging"); });
$("drop-zone").addEventListener("dragleave", () => $("drop-zone").classList.remove("dragging"));
$("drop-zone").addEventListener("drop", event => { event.preventDefault(); $("drop-zone").classList.remove("dragging"); selectFiles(event.dataTransfer.files); });
$("copy-button").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(lastJob ? displayedText(lastJob) : ""); $("copy-button").textContent = "Copied"; }
  catch (error) { showError(error); }
});
$("live-model").addEventListener("change", () => {
  if ($("live-model").value.includes("-en-")) $("live-language").value = "en";
});
window.addEventListener("pagehide", () => {
  if (jobId && busy && lastJob?.kind === "live") {
    void fetch(`/api/jobs/${jobId}/stop`, {method: "POST", headers: {"X-App-Token": token}, keepalive: true}).catch(() => {});
  }
  media?.getTracks().forEach(track => track.stop());
});
async function restoreJob() {
  let id;
  try { id = localStorage.getItem("voice-notes-job"); } catch { return; }
  if (!id) return;
  try {
    const job = await api(`/api/jobs/${id}`);
    jobId = id;
    if (!terminal(job)) {
      setBusy(true);
      // A reload cannot reconnect to the old capture stream. Finish, don't create a second session.
      if (job.kind === "live" && ["loading", "recording"].includes(job.status)) await jsonPost(`/api/jobs/${id}/stop`);
      void poll(id, ++pollGeneration);
    } else render(job);
  } catch { try { localStorage.removeItem("voice-notes-job"); } catch { /* optional */ } }
}
if (navigator.mediaDevices?.addEventListener) navigator.mediaDevices.addEventListener("devicechange", () => { if (!busy) void refreshMicrophones(); });
void refreshMicrophones();
void restoreJob();