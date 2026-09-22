"""Wraps two local TTS engines behind one voice catalog.

- Kokoro (ONNX): higher quality, many languages, but only ~2x faster than
  realtime on a typical CPU.
- Piper (ONNX VITS): roughly 10x faster than Kokoro, so it starts speaking
  almost instantly, at some cost in naturalness.

Both render sentence by sentence, so playback can start as soon as the
first sentence is ready instead of after the whole message.

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
import re
from collections.abc import Iterator
from pathlib import Path

import numpy as np
from kokoro_onnx import Kokoro
from kokoro_onnx.config import EspeakConfig
from piper import PiperVoice

_MODEL_RELEASE_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
_MODEL_FILENAME = "kokoro-v1.0.onnx"
_VOICES_FILENAME = "voices-v1.0.bin"
_KOKORO_SAMPLE_RATE = 24000
# Kokoro trims each sentence's silence, so add a short breath between them.
# (Piper's sentences already end with ~100ms of silence.)
_KOKORO_SENTENCE_GAP_SECONDS = 0.15

_PIPER_RELEASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main"

# Piper voice ID -> language. Each is a separate ~63MB model file.
# (pt_BR-edresson-low is left out: it drops nasal vowels, e.g. "ão".)
PIPER_VOICES = {
    "pt_BR-faber-medium": "pt-br",
    "pt_BR-cadu-medium": "pt-br",
    "pt_BR-jeff-medium": "pt-br",
}

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?…])\s+")

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

KOKORO_VOICES = (
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

VOICES = (*PIPER_VOICES, *KOKORO_VOICES)


def language_for_voice(voice: str) -> str:
    if voice in PIPER_VOICES:
        return PIPER_VOICES[voice]
    return _LANGUAGE_BY_PREFIX[voice[0]]


def engine_for_voice(voice: str) -> str:
    return "piper" if voice in PIPER_VOICES else "kokoro"


class TTSCatalog:
    def __init__(self, kokoro_model_dir: str = "models/kokoro", piper_model_dir: str = "models/piper") -> None:
        model_path, voices_path = _ensure_model_files(Path(kokoro_model_dir))
        espeak_config = EspeakConfig(
            lib_path=_find_espeak_library(), data_path=_find_espeak_data_path()
        )
        self._kokoro = Kokoro(str(model_path), str(voices_path), espeak_config=espeak_config)
        self._piper = {
            voice: PiperVoice.load(str(_ensure_piper_voice(Path(piper_model_dir), voice)))
            for voice in PIPER_VOICES
        }

    def stream(self, voice: str, text: str) -> Iterator[tuple[np.ndarray, int]]:
        """Blocking generator: yields (float32 mono samples, sample rate),
        one sentence at a time, so playback can start before the rest of
        the message has rendered."""
        if voice in self._piper:
            for chunk in self._piper[voice].synthesize(text):
                yield chunk.audio_float_array, chunk.sample_rate
            return

        lang = language_for_voice(voice)
        gap = np.zeros(int(_KOKORO_SENTENCE_GAP_SECONDS * _KOKORO_SAMPLE_RATE), dtype=np.float32)
        for i, sentence in enumerate(split_sentences(text)):
            samples, sample_rate = self._kokoro.create(sentence, voice=voice, lang=lang)
            samples = samples.astype(np.float32)
            yield (np.concatenate([gap, samples]) if i else samples), sample_rate

    def synthesize(self, voice: str, text: str) -> bytes:
        """Blocking call: renders the whole text to 16-bit PCM WAV bytes."""
        chunks = list(self.stream(voice, text))
        sample_rate = chunks[0][1] if chunks else _KOKORO_SAMPLE_RATE
        samples = np.concatenate([c for c, _ in chunks]) if chunks else np.zeros(0, np.float32)
        return to_wav(samples, sample_rate)


def split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENTENCE_BOUNDARY.split(text) if p.strip()]
    return parts or [text]


def to_wav(samples: np.ndarray, sample_rate: int) -> bytes:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm.tobytes())
    return buffer.getvalue()


def _ensure_piper_voice(model_dir: Path, voice: str) -> Path:
    # IDs look like "pt_BR-faber-medium" -> pt/pt_BR/faber/medium/<id>.onnx
    locale, name, quality = voice.split("-")
    remote_dir = f"{_PIPER_RELEASE_URL}/{locale.split('_')[0]}/{locale}/{name}/{quality}"
    model_dir.mkdir(parents=True, exist_ok=True)
    model_path = model_dir / f"{voice}.onnx"
    for path in (model_path, model_dir / f"{voice}.onnx.json"):
        if not path.exists():
            tmp_path = path.with_name(path.name + ".part")
            urllib.request.urlretrieve(f"{remote_dir}/{path.name}", tmp_path)
            os.replace(tmp_path, path)
    return model_path


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
