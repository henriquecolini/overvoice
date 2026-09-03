"""Wraps Pocket TTS to turn text into playable WAV audio.

Models and voice states are loaded lazily and cached, since a guild can pick
any language/voice combination at runtime via admin commands -- we don't
want to eagerly load all of them (each language model is ~450MB) up front.
"""

from __future__ import annotations

import io
import wave

import numpy as np
from pocket_tts import TTSModel

LANGUAGES = ("english", "french", "german", "italian", "portuguese", "spanish")

# The named voice presets Pocket TTS ships, available under every language
# above (voice presets are speaker timbre references shared across
# languages; the language model determines pronunciation/accent).
VOICES = (
    "alba", "anna", "azelma", "bill_boerst", "caro_davy", "charles", "cosette",
    "eponine", "estelle", "eve", "fantine", "george", "giovanni", "jane",
    "javert", "jean", "juergen", "lola", "marius", "mary", "michael", "paul",
    "peter_yearsley", "rafael", "stuart_bell", "vera",
)

# Discord's voice server needs a brief moment to process the "speaking"
# state transition before it starts relaying audio, which otherwise eats
# the first fraction of a second of every clip (audible as a clipped word
# on short messages). Padding with silence gives that warm-up something
# harmless to consume instead of real speech.
_LEADING_SILENCE_SECONDS = 0.35
_TRAILING_SILENCE_SECONDS = 0.15


class TTSCatalog:
    def __init__(self) -> None:
        self._models: dict[str, TTSModel] = {}
        self._voice_states: dict[tuple[str, str], object] = {}

    def synthesize(self, language: str, voice: str, text: str) -> bytes:
        """Blocking call: renders text to 16-bit PCM WAV bytes."""
        model = self._model_for(language)
        voice_state = self._voice_state_for(language, voice)

        audio = model.generate_audio(voice_state, text)
        samples = np.clip(audio.numpy(), -1.0, 1.0)
        pcm = (samples * 32767).astype(np.int16)

        sample_rate = model.sample_rate
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

    def _model_for(self, language: str) -> TTSModel:
        model = self._models.get(language)
        if model is None:
            model = TTSModel.load_model(language=language)
            self._models[language] = model
        return model

    def _voice_state_for(self, language: str, voice: str) -> object:
        key = (language, voice)
        state = self._voice_states.get(key)
        if state is None:
            state = self._model_for(language).get_state_for_audio_prompt(voice)
            self._voice_states[key] = state
        return state
