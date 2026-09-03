"""Wraps Kokoro (ONNX) to turn text into playable WAV audio.

Unlike Pocket TTS's flow/transformer architecture -- which needs an EOS
detector to decide when speech ends, and can misfire on short, context-free
chat messages -- Kokoro is a non-autoregressive model, so short and long
utterances are equally stable.

A single model file covers every language; the language a voice speaks is
implied by its own name (Kokoro voice IDs are `<lang><gender>_<name>`, e.g.
"pf_dora" is the (p)ortuguese-Brazilian (f)emale voice "dora").
"""

from __future__ import annotations

import ctypes.util
import glob
import io
import os
import urllib.request
import wave
from pathlib import Path

import numpy as np
from kokoro_onnx import Kokoro
from kokoro_onnx.config import EspeakConfig

_MODEL_RELEASE_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
_MODEL_FILENAME = "kokoro-v1.0.onnx"
_VOICES_FILENAME = "voices-v1.0.bin"

# Kokoro voice IDs encode their language as a single-letter prefix.
_LANGUAGE_BY_PREFIX = {
    "a": "en-us",
    "b": "en-gb",
    "j": "ja",
    "z": "cmn",
    "e": "es",
    "f": "fr-fr",
    "h": "hi",
    "i": "it",
    "p": "pt-br",
}

VOICES = (
    "af_heart", "af_alloy", "af_aoede", "af_bella", "af_jessica", "af_kore",
    "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
    "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael",
    "am_onyx", "am_puck", "am_santa",
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
    "jf_alpha", "jf_gongitsune", "jf_nezumi", "jf_tebukuro", "jm_kumo",
    "zf_xiaobei", "zf_xiaoni", "zf_xiaoxiao", "zf_xiaoyi",
    "zm_yunjian", "zm_yunxi", "zm_yunxia", "zm_yunyang",
    "ef_dora", "em_alex", "em_santa",
    "ff_siwis",
    "hf_alpha", "hf_beta", "hm_omega", "hm_psi",
    "if_sara", "im_nicola",
    "pf_dora", "pm_alex", "pm_santa",
)

# Discord's voice server needs a brief moment to process the "speaking"
# state transition before it starts relaying audio, which otherwise eats
# the first fraction of a second of every clip (audible as a clipped word
# on short messages). Padding with silence gives that warm-up something
# harmless to consume instead of real speech.
_LEADING_SILENCE_SECONDS = 0.35
_TRAILING_SILENCE_SECONDS = 0.15


def language_for_voice(voice: str) -> str:
    return _LANGUAGE_BY_PREFIX[voice[0]]


class TTSCatalog:
    def __init__(self, model_dir: str = "models/kokoro") -> None:
        model_path, voices_path = _ensure_model_files(Path(model_dir))
        espeak_config = EspeakConfig(
            lib_path=_find_espeak_library(), data_path=_find_espeak_data_path()
        )
        self._kokoro = Kokoro(str(model_path), str(voices_path), espeak_config=espeak_config)

    def synthesize(self, voice: str, text: str) -> bytes:
        """Blocking call: renders text to 16-bit PCM WAV bytes."""
        samples, sample_rate = self._kokoro.create(text, voice=voice, lang=language_for_voice(voice))
        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)

        lead_silence = np.zeros(int(_LEADING_SILENCE_SECONDS * sample_rate), dtype=np.int16)
        trail_silence = np.zeros(int(_TRAILING_SILENCE_SECONDS * sample_rate), dtype=np.int16)
        pcm = np.concatenate([lead_silence, pcm, trail_silence])

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm.tobytes())
        return buffer.getvalue()


def _ensure_model_files(model_dir: Path) -> tuple[Path, Path]:
    model_dir.mkdir(parents=True, exist_ok=True)
    model_path = model_dir / _MODEL_FILENAME
    voices_path = model_dir / _VOICES_FILENAME
    for filename, path in ((_MODEL_FILENAME, model_path), (_VOICES_FILENAME, voices_path)):
        if not path.exists():
            urllib.request.urlretrieve(f"{_MODEL_RELEASE_URL}/{filename}", path)
    return model_path, voices_path


def _find_espeak_library() -> str:
    # The espeakng-loader wheel kokoro-onnx depends on ships a prebuilt
    # libespeak-ng with a broken baked-in data path, so we always point at
    # the system package (apt/dnf installed) instead of its bundled copy.
    found = ctypes.util.find_library("espeak-ng")
    if found:
        return found
    for pattern in ("/usr/lib*/libespeak-ng.so*", "/usr/lib/*/libespeak-ng.so*"):
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
    raise RuntimeError("libespeak-ng not found -- is the espeak-ng system package installed?")


def _find_espeak_data_path() -> str:
    candidates = [
        "/usr/share/espeak-ng-data",
        "/usr/lib/espeak-ng-data",
        *glob.glob("/usr/lib/*/espeak-ng-data"),
    ]
    for path in candidates:
        if os.path.isdir(path):
            return path
    raise RuntimeError("espeak-ng-data not found -- is the espeak-ng system package installed?")
