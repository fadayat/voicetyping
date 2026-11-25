"""Run a secret-safe Gemini transcription smoke test from the Windows package."""
from __future__ import annotations

import argparse
from collections.abc import Callable, MutableMapping
import os
from pathlib import Path
import sys

import soundfile as sf

from .engine import VoiceTypingApp, create_client
from .selftest import _write_report


EXPECTED_TOKENS = ("voice", "typing", "integration", "test")


class _Panel:
    def show(self, *_args, **_kwargs) -> None:
        pass


def _transcribe(api_key: str, audio, sample_rate: float) -> str:
    app = VoiceTypingApp(
        create_client(api_key),
        _Panel(),
        copy_action=lambda _text: None,
        fallback_models=(),
    )
    return app._batch_transcribe(audio, sample_rate)


def run_gemini_smoke(
    audio_path: Path,
    report_path: Path,
    *,
    platform_name: str = sys.platform,
    environ: MutableMapping[str, str] | None = None,
    audio_reader: Callable = sf.read,
    transcriber: Callable[[str, object, float], str] = _transcribe,
) -> int:
    """Transcribe a fixed fixture without persisting the key or transcript."""
    checks: list[dict[str, str]] = []
    success = True

    def result(name: str, status: str) -> None:
        nonlocal success
        checks.append({"name": name, "status": status})
        if status != "passed":
            success = False

    def finish() -> int:
        try:
            _write_report(Path(report_path), checks, success)
        except OSError:
            return 1
        return 0 if success else 1

    environment = os.environ if environ is None else environ
    api_key = environment.pop("GEMINI_API_KEY", "").strip()

    if platform_name != "win32":
        result("windows_platform", "failed")
        api_key = ""
        return finish()
    result("windows_platform", "passed")

    if not api_key or "\n" in api_key or "\r" in api_key:
        result("api_key_present", "failed")
        api_key = ""
        return finish()
    result("api_key_present", "passed")

    try:
        audio, sample_rate = audio_reader(
            str(audio_path),
            dtype="int16",
            always_2d=True,
        )
        if getattr(audio, "size", 0) <= 0 or float(sample_rate) <= 0:
            raise ValueError("empty fixture")
    except Exception:
        result("audio_fixture", "failed")
        api_key = ""
        return finish()
    result("audio_fixture", "passed")

    try:
        transcript = transcriber(api_key, audio, float(sample_rate))
    except Exception:
        result("gemini_transcription", "failed")
        api_key = ""
        return finish()
    finally:
        api_key = ""

    if not isinstance(transcript, str) or not transcript.strip():
        result("gemini_transcription", "failed")
        transcript = ""
        return finish()
    result("gemini_transcription", "passed")

    normalized = transcript.casefold()
    matched = sum(token in normalized for token in EXPECTED_TOKENS)
    transcript = ""
    result("expected_phrase", "passed" if matched >= 2 else "failed")
    return finish()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    arguments = parser.parse_args(argv)
    return run_gemini_smoke(arguments.audio, arguments.report)


if __name__ == "__main__":
    raise SystemExit(main())
