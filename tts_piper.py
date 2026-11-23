"""Motor TTS local persistente basado en Piper con pulido acústico en memoria (sin latencia de red)."""

import atexit
import json
import os
import re
import subprocess
import threading
import numpy as np
import soundfile as sf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PIPER_EXE = os.path.join(BASE_DIR, "piper_voice", "piper", "piper.exe")
DEFAULT_PIPER_MODEL = os.path.join(BASE_DIR, "piper_voice", "es_MX-claude-14947-epoch-high.onnx")
DEFAULT_PIPER_ESPEAK = os.path.join(BASE_DIR, "piper_voice", "piper", "espeak-ng-data")

DEFAULT_LENGTH_SCALE = "0.90"
DEFAULT_NOISE_SCALE = "0.55"
DEFAULT_NOISE_W = "0.72"
DEFAULT_SENTENCE_SILENCE = "0.10"

_piper_lock = threading.Lock()
_piper_proc: subprocess.Popen | None = None
_piper_cfg_activa: tuple[str, str, str, str, str] | None = None


def limpiar_texto_tts(texto: str) -> str:
    """Limpia markdown, enlaces o símbolos que sonarían robóticos al sintetizarse en voz."""
    t = str(texto or "").strip()
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"[*_`#>~]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _config_piper_actual(model_path: str | None = None) -> tuple[str, str, str, str, str]:
    modelo = model_path or os.getenv("PIPER_MODEL") or DEFAULT_PIPER_MODEL
    length_scale = (os.getenv("PIPER_LENGTH_SCALE") or DEFAULT_LENGTH_SCALE).strip()
    noise_scale = (os.getenv("PIPER_NOISE_SCALE") or DEFAULT_NOISE_SCALE).strip()
    noise_w = (os.getenv("PIPER_NOISE_W") or DEFAULT_NOISE_W).strip()
    sentence_silence = (os.getenv("PIPER_SENTENCE_SILENCE") or DEFAULT_SENTENCE_SILENCE).strip()
    return modelo, length_scale, noise_scale, noise_w, sentence_silence


def piper_disponible(model_path: str | None = None) -> bool:
    """Devuelve True si el binario de Piper y el modelo ONNX existen en disco."""
    modelo = model_path or os.getenv("PIPER_MODEL") or DEFAULT_PIPER_MODEL
    return os.path.isfile(DEFAULT_PIPER_EXE) and os.path.isfile(modelo)


def _asegurar_proceso(model_path: str | None = None) -> subprocess.Popen | None:
    """Inicia o reutiliza el proceso persistente de Piper con el modelo cargado en RAM."""
    global _piper_proc, _piper_cfg_activa

    cfg = _config_piper_actual(model_path)
    modelo, length_scale, noise_scale, noise_w, sentence_silence = cfg

    if _piper_proc is not None and _piper_proc.poll() is None and _piper_cfg_activa == cfg:
        return _piper_proc

    cerrar_piper()
    if not piper_disponible(modelo):
        return None

    cmd = [
        DEFAULT_PIPER_EXE,
        "-m",
        modelo,
        "--json-input",
        "-q",
        "--length_scale",
        length_scale,
        "--noise_scale",
        noise_scale,
        "--noise_w",
        noise_w,
        "--sentence_silence",
        sentence_silence,
    ]
    if os.path.isdir(DEFAULT_PIPER_ESPEAK):
        cmd.extend(["--espeak_data", DEFAULT_PIPER_ESPEAK])

    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)
    env_piper = os.environ.copy()
    env_piper.setdefault("OMP_NUM_THREADS", "2")
    _piper_proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        env=env_piper,
        creationflags=creationflags,
    )
    _piper_cfg_activa = cfg
    return _piper_proc


def _pulir_audio_jarvis(archivo_wav: str):
    """Recorta silencios muertos y aplica ecualización + presencia táctil en <10ms."""
    if (os.getenv("JARVIS_VOICE_DSP") or "1").strip() == "0":
        return
    try:
        audio, sr = sf.read(archivo_wav, dtype="float32")
        if audio.ndim > 1:
            audio = np.mean(audio, axis=1)
        if len(audio) < int(sr * 0.05):
            return

        # 1. Recortar silencio inicial/final para arranque instantáneo
        activos = np.flatnonzero(np.abs(audio) > 0.008)
        if len(activos) > 0:
            pad = int(sr * 0.015)
            ini = max(0, int(activos[0]) - pad)
            fin = min(len(audio), int(activos[-1]) + pad)
            audio = audio[ini:fin]

        # 2. Ecualización espectral suave (limpia graves barrosos, suma calidez y presencia)
        n = len(audio)
        freqs = np.fft.rfftfreq(n, d=1.0 / sr)
        spec = np.fft.rfft(audio)

        gain = np.ones_like(freqs, dtype=np.float32)
        # Corte suave bajo 100 Hz
        gain *= np.clip((freqs - 55.0) / 55.0, 0.15, 1.0)
        # Calidez vocal centrada en 210 Hz (+1.8 dB)
        gain *= 1.0 + 0.22 * np.exp(-0.5 * ((freqs - 210.0) / 85.0) ** 2)
        # Presencia clara estilo HUD en 3.1 kHz (+2.6 dB)
        gain *= 1.0 + 0.34 * np.exp(-0.5 * ((freqs - 3100.0) / 950.0) ** 2)
        # Suavizado de sibilancia/brillo metálico sobre 8.5 kHz
        gain *= np.where(freqs > 8500.0, np.clip(1.0 - (freqs - 8500.0) / 5000.0, 0.55, 1.0), 1.0)

        procesado = np.fft.irfft(spec * gain, n=n).astype(np.float32)

        # 3. Micro-espacio de cabina muy sutil (6.5 ms) para timbre natural sin eco
        retardo = max(1, int(sr * 0.0065))
        if len(procesado) > retardo:
            wet = np.zeros_like(procesado)
            wet[retardo:] = procesado[:-retardo]
            procesado = 0.92 * procesado + 0.08 * wet

        # 4. Compresión suave + normalización consistente (-1.5 dBFS) + fade anti-click
        procesado = np.tanh(procesado * 1.18).astype(np.float32)
        pico = float(np.max(np.abs(procesado)))
        if pico > 1e-4:
            procesado = procesado * (0.84 / pico)

        fade = min(int(sr * 0.008), len(procesado) // 4)
        if fade > 1:
            procesado[:fade] *= np.linspace(0.0, 1.0, fade, dtype=np.float32)
            procesado[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)

        sf.write(archivo_wav, procesado, sr, subtype="PCM_16")
    except Exception:
        pass


def sintetizar_piper(texto: str, archivo_wav: str, model_path: str | None = None) -> bool:
    """Sintetiza `texto` a `archivo_wav` usando el proceso local persistente de Piper."""
    texto_limpio = limpiar_texto_tts(texto)
    if not texto_limpio:
        return False

    os.makedirs(os.path.dirname(os.path.abspath(archivo_wav)), exist_ok=True)

    with _piper_lock:
        for _ in range(2):
            try:
                proc = _asegurar_proceso(model_path)
                if proc is None or proc.stdin is None or proc.stdout is None:
                    return False

                payload = json.dumps({"text": texto_limpio, "output_file": archivo_wav}, ensure_ascii=False)
                proc.stdin.write(payload + "\n")
                proc.stdin.flush()

                linea = proc.stdout.readline()
                if linea and os.path.exists(archivo_wav) and os.path.getsize(archivo_wav) > 256:
                    _pulir_audio_jarvis(archivo_wav)
                    return True
            except Exception as e:
                print(f"[Aviso Piper TTS]: {e}")
            cerrar_piper()

    return False


def cerrar_piper():
    """Detiene de forma segura el proceso persistente de Piper."""
    global _piper_proc, _piper_cfg_activa
    proc = _piper_proc
    _piper_proc = None
    _piper_cfg_activa = None
    if proc is None:
        return
    try:
        if proc.stdin:
            proc.stdin.close()
    except Exception:
        pass
    try:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


atexit.register(cerrar_piper)
