"""Speech to text for Telegram voice notes: faster-whisper, runs on this Mac (free, private, understands Swedish and English).
The model (about 460 MB) downloads on first use and stays loaded afterwards."""
from __future__ import annotations
import logging, subprocess, threading

log = logging.getLogger("jarvis.stt")
MODEL = "small"
_model, _lock = None, threading.Lock()


def _pcm(audio: bytes):
    """Telegram voice notes are Opus in Ogg: ffmpeg turns any input into 16 kHz mono floats for the model."""
    import numpy as np
    out = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-f", "f32le", "-ac", "1", "-ar", "16000", "pipe:1"],
                         input=audio, capture_output=True, timeout=60, check=True).stdout
    return np.frombuffer(out, dtype=np.float32)


def transcribe(audio: bytes) -> str:
    global _model
    pcm = _pcm(audio)
    with _lock:
        if _model is None:
            from faster_whisper import WhisperModel
            _model = WhisperModel(MODEL, device="cpu", compute_type="int8")
        segments, info = _model.transcribe(pcm, beam_size=1, vad_filter=True, language=None,
                                           initial_prompt="Jarvis, mäklare, Mäklarkontroll, mejl, faktura.")
        text = " ".join(s.text.strip() for s in segments).strip()
    log.info("transcribed %d bytes (%s): %r", len(audio), info.language, text[:60])
    return text
