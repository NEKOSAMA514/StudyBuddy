import subprocess
import sys
import time
import pytest
from app.errors import AppError
from app.util.media_process import command


@pytest.mark.parametrize("tool", ["ffmpeg", "ffprobe", sys.executable])
def test_command_preserves_protocol_scope_and_arguments(monkeypatch, tool):
    calls = []

    class Process:
        returncode = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def communicate(self, timeout=None):
            return b"result", b""

        def poll(self):
            return 0

    def popen(args, **kwargs):
        calls.append((args, kwargs))
        return Process()

    monkeypatch.setattr(subprocess, "Popen", popen)
    assert command([tool, "-i", "source"]) == b"result"
    prefix = ["-protocol_whitelist", "file,pipe"] if tool in {"ffmpeg", "ffprobe"} else []
    assert calls == [([tool, *prefix, "-i", "source"], {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE})]


@pytest.mark.parametrize("failure,code", [(FileNotFoundError(), "tool_missing"),
                                         (subprocess.CalledProcessError(1, ["tool"]), "invalid_media")])
def test_command_preserves_error_codes(monkeypatch, failure, code):
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(subprocess, "Popen", fail)
    with pytest.raises(AppError) as caught:
        command(["tool"])
    assert caught.value.code == code


def test_command_timeout_reaps_process_without_relaxing_wait(monkeypatch):
    original = subprocess.Popen
    processes = []

    def popen(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", popen)
    start = time.monotonic()
    with pytest.raises(AppError) as caught:
        command([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.3)
    assert caught.value.code == "media_timeout" and time.monotonic() - start < 3
    assert processes[0].poll() is not None


def test_backend_fixture_blocks_unmocked_http():
    import asyncio
    import httpx
    with pytest.raises(AssertionError, match="live requests are disabled"):
        httpx.get("https://fixture.invalid")

    async def request():
        async with httpx.AsyncClient() as client:
            await client.get("https://fixture.invalid")
    with pytest.raises(AssertionError, match="live requests are disabled"):
        asyncio.run(request())
