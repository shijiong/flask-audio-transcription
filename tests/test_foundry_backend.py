"""SDK import failures should identify the environment that needs fixing."""

import builtins
from contextlib import ExitStack
import gc
import hashlib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import struct
import sys
from types import SimpleNamespace

import pytest

from foundry_backend import FoundryBackend, NativeLiveSession, SpeechEvent, copy_segment


@pytest.mark.parametrize("platform,package", [
    ("linux", "foundry-local-sdk"),
    ("win32", "foundry-local-sdk-winml"),
])
def test_missing_sdk_reports_install_command(monkeypatch, platform, package):
    original_import = builtins.__import__

    def missing_sdk(name, *args, **kwargs):
        if name == "foundry_local_sdk":
            raise ModuleNotFoundError("No module named 'foundry_local_sdk'", name=name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(builtins, "__import__", missing_sdk)
    backend = FoundryBackend()
    with pytest.raises(RuntimeError) as error:
        backend._initialize(lambda message: None)

    message = str(error.value)
    requirements = Path(__file__).resolve().parents[1] / "requirements.txt"
    assert package in message
    assert sys.executable in message
    assert str(requirements) in message
    assert "-m pip install -r" in message
    assert "Restart Flask" in message
    assert isinstance(error.value.__cause__, ModuleNotFoundError)
    assert backend._manager is None


def test_sdk_dependency_import_failure_is_not_reported_as_missing_sdk(monkeypatch):
    original_import = builtins.__import__
    failure = ModuleNotFoundError("No module named 'cffi'", name="cffi")

    def missing_dependency(name, *args, **kwargs):
        if name == "foundry_local_sdk":
            raise failure
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_dependency)
    with pytest.raises(ModuleNotFoundError) as error:
        FoundryBackend()._initialize(lambda message: None)
    assert error.value is failure


@pytest.mark.parametrize("kind,final", [(1, False), (2, True), (0, True)])
def test_speech_event_copies_only_text_and_confirmation(kind, final):
    # No timestamp attributes: copying must not attempt to access any.
    assert copy_segment(SimpleNamespace(text="实时文字", kind=kind)) == SpeechEvent("实时文字", final=final)


@pytest.mark.parametrize("result_kind", ["speech", "text"])
def test_native_stream_yields_text_without_timestamps(monkeypatch, result_kind):
    from contextlib import nullcontext

    class Segment:
        def __init__(self, text, kind):
            self.text, self.kind = text, kind

    class SpeechResult:
        text = ""
        segments = [SimpleNamespace(text="Hello world.")]

    class TextResult:
        text = "Hello world."

    # Stub the SDK module so tests do not require the native runtime.
    monkeypatch.setitem(sys.modules, "foundry_local_sdk.items", SimpleNamespace(
        SpeechSegmentItem=Segment, SpeechResultItem=SpeechResult, TextItem=TextResult))

    class Stream:
        final_response = nullcontext([SpeechResult() if result_kind == "speech" else TextResult()])

        def __iter__(self):
            return iter([Segment("Hel", 1), Segment("Hello", 1), Segment("Hello", 2), Segment(" world.", 2)])

    live = NativeLiveSession.__new__(NativeLiveSession)
    live._request = object()
    live._session = SimpleNamespace(process_streaming_request=lambda request: nullcontext(Stream()))
    assert list(live.events()) == [
        SpeechEvent("Hel", final=False), SpeechEvent("Hello", final=False),
        SpeechEvent("Hello"), SpeechEvent(" world."), SpeechEvent("Hello world.", aggregate=True),
    ]


def test_native_pcm_queue_preserves_buffers_until_session_close(monkeypatch):
    # SDK 2.1 SetBytes borrows Python memory even after queue ownership transfers.
    # Constant/reused test bytes hide the bug; allocate different PCM per chunk.
    try:
        sdk_version = version("foundry-local-sdk")
    except PackageNotFoundError:
        pytest.skip("Native SDK not installed")
    if sys.platform.startswith("linux") and sdk_version == "2.1.0":
        monkeypatch.setenv("CI", "1")
    from foundry_local_sdk.item_queue import ItemQueue

    live = NativeLiveSession.__new__(NativeLiveSession)
    live._stack = ExitStack()
    live._queue = live._stack.enter_context(ItemQueue())
    live._audio_items = []
    expected = bytearray()
    try:
        for sequence in range(128):
            pcm = struct.pack("<1600h", *(sequence * 100 + index % 100 for index in range(1600)))
            expected.extend(pcm)
            live.append(pcm)
        del pcm
        gc.collect()
        actual = bytearray()
        for _ in range(128):
            item = live._queue.try_pop()
            assert item is not None
            actual.extend(item.data)
        assert len(actual) == len(expected)
        assert hashlib.sha256(actual).digest() == hashlib.sha256(expected).digest(), (
            "PCM backing memory was freed/reused after ItemQueue.push"
        )
    finally:
        live.close()
    assert live._audio_items == []


def test_close_keeps_pcm_alive_while_native_handles_release():
    live = NativeLiveSession.__new__(NativeLiveSession)
    live._stack = ExitStack()
    item = SimpleNamespace(data=b"\x01\0" * 1600)
    live._audio_items = [item]
    released = []

    def close_native():
        assert live._audio_items == [item]
        released.append(True)

    live._stack.callback(close_native)
    live.close()
    assert released == [True]
    assert live._audio_items == []
    live.close()  # Closing twice remains safe.