"""Text-to-speech: voices rendered to cached mp3 files that Twilio <Play>s and WhatsApp can carry.
Twilio's own <Say> only knows Polly voices, so anything else has to be rendered here and served from /audio.
 - thomas, luke: edge-tts (online, free)
 - adam: Kokoro (local, free; needs `pip install -e .[kokoro]` and the two model files in models/)"""
from __future__ import annotations
import asyncio, hashlib, hmac, logging, os, threading, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from xml.sax.saxutils import escape
from .config import Config

log = logging.getLogger("jarvis.tts")

VOICES = {"thomas": "en-GB-ThomasNeural", "luke": "en-ZA-LukeNeural", "adam": "am_adam"}
KOKORO = {"adam"}
MODELS = Path(os.environ.get("JARVIS_MODELS_DIR", "models"))
_kokoro, _kokoro_lock = None, threading.Lock()
AUDIO_DIR = Path(os.environ.get("JARVIS_AUDIO_DIR", "audio"))
KEEP_SECONDS = 7 * 86400
_pool = ThreadPoolExecutor(max_workers=2)


def _name(engine: str, text: str) -> str:
    # keyed hash: the file name is a capability URL, and the text (email summaries) must not be guessable from it
    key = (Config.env("JARVIS_CHAT_TOKEN") or "jarvis").encode()
    return hmac.new(key, f"{engine}|{text}".encode(), hashlib.sha256).hexdigest()[:32] + ".mp3"


def _synth(text: str, voice: str, path: Path) -> None:
    import edge_tts
    asyncio.run(edge_tts.Communicate(text, voice).save(str(path)))


def _synth_kokoro(text: str, voice: str, path: Path) -> None:
    global _kokoro
    import lameenc
    import numpy as np
    with _kokoro_lock:      # one ONNX session, one synthesis at a time
        if _kokoro is None:
            from kokoro_onnx import Kokoro
            _kokoro = Kokoro(str(MODELS / "kokoro-v1.0.onnx"), str(MODELS / "voices-v1.0.bin"))
        samples, rate = _kokoro.create(text, voice=voice, speed=1.0, lang="en-us")
    enc = lameenc.Encoder()
    enc.set_bit_rate(64)
    enc.set_in_sample_rate(rate)
    enc.set_channels(1)
    enc.set_quality(2)
    pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes()
    path.write_bytes(enc.encode(pcm) + enc.flush())


def render(cfg: Config, text: str, timeout: float = 15) -> str | None:
    """File name of the mp3 for `text` in the configured voice, or None (use Polly) if this engine can't speak it."""
    engine = cfg["voice"].get("engine", "polly")
    if engine not in VOICES or not text.strip():
        return None
    name = _name(engine, text)
    path = AUDIO_DIR / name
    if path.exists():
        return name
    try:
        AUDIO_DIR.mkdir(exist_ok=True)
        tmp = path.with_suffix(".part")
        _pool.submit(_synth_kokoro if engine in KOKORO else _synth, text, VOICES[engine], tmp).result(timeout)
        tmp.replace(path)
        _cleanup()
        return name
    except Exception:
        log.exception("tts failed, falling back to Polly")
        return None


def _cleanup() -> None:
    cutoff = time.time() - KEEP_SECONDS
    for f in AUDIO_DIR.glob("*.mp3"):
        if f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)


def audio_url(name: str) -> str:
    return Config.env("PUBLIC_URL").rstrip("/") + "/audio/" + name


def tag(cfg: Config, text: str) -> str:
    """TwiML that speaks `text`: <Play> of our rendered voice, or Twilio's Polly <Say> as the fallback."""
    name = render(cfg, text)
    if name:
        return f"<Play>{escape(audio_url(name))}</Play>"
    v = cfg["voice"]
    return f'<Say voice="{v["twilio_voice"]}" language="{v["language"]}">{escape(text)}</Say>'
