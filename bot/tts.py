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
import json
import os
import re
import threading
import urllib.request
import wave
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import onnxruntime as ort
from kokoro_onnx import Kokoro
from kokoro_onnx.config import EspeakConfig
from kokoro_onnx.trim import trim as trim_audio
from phonemizer.backend import EspeakBackend
from piper import PiperVoice
from piper.config import PiperConfig

_MODEL_RELEASE_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
_MODEL_FILENAME = "kokoro-v1.0.onnx"
_VOICES_FILENAME = "voices-v1.0.bin"
_KOKORO_SAMPLE_RATE = 24000
# Each sentence is rendered with its surrounding silence trimmed, so add a
# short breath between them.
_SENTENCE_GAP_SECONDS = 0.15

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
        self._kokoro = Kokoro.from_session(_load_session(model_path), str(voices_path), espeak_config=espeak_config)
        self._piper = {
            voice: _load_piper_voice(_ensure_piper_voice(Path(piper_model_dir), voice))
            for voice in PIPER_VOICES
        }
        # kokoro-onnx phonemizes via phonemizer.phonemize(), which builds a
        # new espeak backend on every call (~80ms per sentence); keep one per
        # language instead. espeak-ng isn't thread-safe, and synthesis runs in
        # worker threads, so every phonemization goes through this lock.
        self._espeak_backends: dict[str, EspeakBackend] = {}
        self._phonemize_lock = threading.Lock()

        # Each model's first run is slower (ONNX Runtime sets up lazily), so
        # pay that at startup rather than on someone's first message.
        for voice in ("pf_dora", *PIPER_VOICES):
            self.synthesize(voice, "ok")

    def stream(self, voice: str, text: str) -> Iterator[tuple[np.ndarray, int]]:
        """Blocking generator: yields (float32 mono samples, sample rate),
        one sentence at a time, so playback can start before the rest of
        the message has rendered."""
        for i, (samples, sample_rate) in enumerate(self._render_sentences(voice, text)):
            if i:
                gap = np.zeros(int(_SENTENCE_GAP_SECONDS * sample_rate), dtype=np.float32)
                samples = np.concatenate([gap, samples])
            yield samples, sample_rate

    def _render_sentences(self, voice: str, text: str) -> Iterator[tuple[np.ndarray, int]]:
        """Yields each sentence's audio with its surrounding silence trimmed."""
        if voice in self._piper:
            piper_voice = self._piper[voice]
            with self._phonemize_lock:
                sentences = piper_voice.phonemize(text)
            for phonemes in sentences:
                if phonemes:
                    audio = piper_voice.phoneme_ids_to_audio(piper_voice.phonemes_to_ids(phonemes))
                    sample_rate = piper_voice.config.sample_rate
                    yield _trim_silence(audio, sample_rate), sample_rate
            return

        # kokoro-onnx only splits text past 510 phonemes -- about a whole
        # chat message -- so split by sentence here to stream at all.
        # (Kokoro trims each sentence's silence itself.)
        lang = language_for_voice(voice)
        for sentence in split_sentences(text):
            phonemes = self._kokoro_phonemes(sentence, lang)
            if phonemes:
                samples, sample_rate = self._kokoro.create(phonemes, voice=voice, is_phonemes=True)
                yield samples.astype(np.float32), sample_rate

    def synthesize(self, voice: str, text: str) -> bytes:
        """Blocking call: renders the whole text to 16-bit PCM WAV bytes."""
        chunks = list(self.stream(voice, text))
        sample_rate = chunks[0][1] if chunks else _KOKORO_SAMPLE_RATE
        samples = np.concatenate([c for c, _ in chunks]) if chunks else np.zeros(0, np.float32)
        return to_wav(samples, sample_rate)

    def _kokoro_phonemes(self, text: str, lang: str) -> str:
        with self._phonemize_lock:
            backend = self._espeak_backends.get(lang)
            if backend is None:
                backend = EspeakBackend(lang, preserve_punctuation=True, with_stress=True)
                self._espeak_backends[lang] = backend
            phonemes = backend.phonemize([text.strip()])[0]
        vocab = self._kokoro.tokenizer.vocab
        return "".join(p for p in phonemes if p in vocab).strip()


def _trim_silence(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    # Some Piper voices (cadu, jeff) open every sentence with ~0.4-0.6s of
    # low-level noise, which delays speech and stretches sentence gaps.
    # 30dB below the loudest ~23ms stretch cuts that noise without clipping
    # quiet word starts like "s" or "f".
    trimmed, _ = trim_audio(audio, top_db=30, frame_length=512, hop_length=128)
    # The cut lands mid-waveform; a 5ms fade at each edge avoids a click.
    fade = np.linspace(0.0, 1.0, min(int(0.005 * sample_rate), len(trimmed) // 2), dtype=np.float32)
    trimmed = trimmed.copy()
    trimmed[: len(fade)] *= fade
    trimmed[len(trimmed) - len(fade):] *= fade[::-1]
    return trimmed


def _load_session(model_path: Path) -> ort.InferenceSession:
    # ONNX Runtime's memory arena keeps a buffer for every input size it has
    # seen, so with chat messages of all lengths it roughly doubles the bot's
    # memory (~0.75GB -> ~1.6GB); without it memory stays flat, at no
    # measurable speed cost.
    options = ort.SessionOptions()
    options.enable_cpu_mem_arena = False
    return ort.InferenceSession(str(model_path), options, providers=["CPUExecutionProvider"])


def _load_piper_voice(model_path: Path) -> PiperVoice:
    with open(f"{model_path}.json", encoding="utf-8") as config_file:
        config = PiperConfig.from_dict(json.load(config_file))
    return PiperVoice(session=_load_session(model_path), config=config)


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
