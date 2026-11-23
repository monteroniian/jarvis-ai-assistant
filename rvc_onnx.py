"""Inferencia local RVC v2 sobre ONNX Runtime (sin PyTorch ni Fairseq)."""

import os
import threading
import numpy as np
import onnxruntime as ort
import pyworld
import soundfile as sf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RVC_MODELS_DIR = os.path.join(BASE_DIR, "rvc_voice", "models")

_rvc_lock = threading.Lock()
_sessions: dict[str, object] = {}


def _rutas_modelos() -> tuple[str, str]:
    modelo_rvc = (os.getenv("RVC_MODEL_PATH") or os.path.join(RVC_MODELS_DIR, "jarvis_rvc.onnx")).strip()
    vec_env = (os.getenv("RVC_VEC_PATH") or "").strip()
    if vec_env:
        return modelo_rvc, vec_env
    v2 = os.path.join(RVC_MODELS_DIR, "vec-768-layer-12.onnx")
    v1 = os.path.join(RVC_MODELS_DIR, "vec-256-layer-9.onnx")
    return modelo_rvc, (v2 if os.path.isfile(v2) else v1)


def rvc_disponible() -> bool:
    """Devuelve True si existen tanto el modelo RVC ONNX como el extractor ContentVec ONNX."""
    modelo_rvc, modelo_vec = _rutas_modelos()
    return os.path.isfile(modelo_rvc) and os.path.isfile(modelo_vec)


def _cargar_sesiones() -> tuple[ort.InferenceSession, ort.InferenceSession] | None:
    modelo_rvc, modelo_vec = _rutas_modelos()
    if not (os.path.isfile(modelo_rvc) and os.path.isfile(modelo_vec)):
        return None

    clave = f"{modelo_rvc}|{modelo_vec}"
    if _sessions.get("clave") == clave:
        return _sessions["rvc"], _sessions["vec"]  # type: ignore[return-value]

    opts = ort.SessionOptions()
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    providers = ["CPUExecutionProvider"]

    rvc_sess = ort.InferenceSession(modelo_rvc, sess_options=opts, providers=providers)
    vec_sess = ort.InferenceSession(modelo_vec, sess_options=opts, providers=providers)
    _sessions.clear()
    _sessions["clave"] = clave
    _sessions["rvc"] = rvc_sess
    _sessions["vec"] = vec_sess
    return rvc_sess, vec_sess


def _remuestrear_lineal(audio: np.ndarray, sr_origen: int, sr_destino: int) -> np.ndarray:
    if sr_origen == sr_destino or len(audio) == 0:
        return audio.astype(np.float32)
    n_dest = max(1, int(round(len(audio) * sr_destino / sr_origen)))
    x_old = np.linspace(0.0, 1.0, num=len(audio), endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_dest, endpoint=False)
    return np.interp(x_new, x_old, audio).astype(np.float32)


def _extraer_f0_pyworld(audio_16k: np.ndarray, longitud_objetivo: int, f0_up_key: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
    """Extrae pitch continuo (Hz) y cuantizado (1..255) alineados a 100 Hz (10ms)."""
    audio_f64 = audio_16k.astype(np.float64)
    f0, t = pyworld.dio(audio_f64, 16000, f0_floor=50.0, f0_ceil=1100.0, frame_period=10.0)
    f0 = pyworld.stonemask(audio_f64, f0, t, 16000)

    if len(f0) != longitud_objetivo:
        x_old = np.linspace(0.0, 1.0, num=max(1, len(f0)), endpoint=False)
        x_new = np.linspace(0.0, 1.0, num=longitud_objetivo, endpoint=False)
        f0 = np.interp(x_new, x_old, f0) if len(f0) > 0 else np.zeros(longitud_objetivo)

    if f0_up_key != 0.0:
        f0 = f0 * (2.0 ** (f0_up_key / 12.0))

    f0_mel_min = 1127.0 * np.log(1.0 + 50.0 / 700.0)
    f0_mel_max = 1127.0 * np.log(1.0 + 1100.0 / 700.0)
    f0_mel = 1127.0 * np.log(1.0 + f0 / 700.0)
    mask = f0 > 0
    f0_mel[mask] = (f0_mel[mask] - f0_mel_min) * 254.0 / (f0_mel_max - f0_mel_min) + 1.0
    f0_mel = np.clip(f0_mel, 1.0, 255.0)
    f0_mel[~mask] = 0.0

    pitch = np.rint(f0_mel).astype(np.int64)
    pitchf = f0.astype(np.float32)
    return pitch[None, :], pitchf[None, :]


def convertir_audio_rvc(wav_entrada: str, wav_salida: str) -> bool:
    """Convierte el timbre de `wav_entrada` usando el modelo RVC ONNX local."""
    if not rvc_disponible() or not os.path.isfile(wav_entrada):
        return False

    try:
        audio, sr = sf.read(wav_entrada, dtype="float32")
        if audio.ndim > 1:
            audio = np.mean(audio, axis=1)

        audio_16k = _remuestrear_lineal(audio, sr, 16000)
        f0_up_key = float((os.getenv("RVC_F0_UP_KEY") or "0").strip())
        sid = int((os.getenv("RVC_SID") or "0").strip())
        sr_salida = int((os.getenv("RVC_SAMPLE_RATE") or "40000").strip())

        with _rvc_lock:
            sesiones = _cargar_sesiones()
            if sesiones is None:
                return False
            rvc_sess, vec_sess = sesiones

            vec_inp = vec_sess.get_inputs()[0]
            vec_tensor = audio_16k[None, None, :] if len(vec_inp.shape) == 3 else audio_16k[None, :]
            feats = vec_sess.run(None, {vec_inp.name: vec_tensor.astype(np.float32)})[0]

            # ContentVec opera a 50 Hz; RVC espera 100 Hz (duplicando frames en el eje temporal)
            phone = np.repeat(feats, 2, axis=1).astype(np.float32)
            t_frames = int(phone.shape[1])
            pitch, pitchf = _extraer_f0_pyworld(audio_16k, t_frames, f0_up_key=f0_up_key)

            ort_inputs = {}
            for inp in rvc_sess.get_inputs():
                nombre = inp.name
                if nombre in ("phone", "feats"):
                    ort_inputs[nombre] = phone
                elif nombre == "phone_lengths":
                    ort_inputs[nombre] = np.array([t_frames], dtype=np.int64)
                elif nombre == "pitch":
                    ort_inputs[nombre] = pitch
                elif nombre == "pitchf":
                    ort_inputs[nombre] = pitchf
                elif nombre in ("ds", "sid"):
                    ort_inputs[nombre] = np.array([sid], dtype=np.int64)
                elif nombre == "rnd":
                    ort_inputs[nombre] = np.random.randn(1, 192, t_frames).astype(np.float32)

            audio_out = rvc_sess.run(None, ort_inputs)[0].squeeze().astype(np.float32)

        pico = float(np.max(np.abs(audio_out))) if len(audio_out) else 0.0
        if pico > 0.99:
            audio_out = audio_out * (0.95 / pico)

        os.makedirs(os.path.dirname(os.path.abspath(wav_salida)), exist_ok=True)
        sf.write(wav_salida, audio_out, sr_salida, subtype="PCM_16")
        return os.path.exists(wav_salida) and os.path.getsize(wav_salida) > 256
    except Exception as e:
        print(f"[Aviso RVC ONNX]: {e}")
        return False


def cerrar_rvc():
    """Libera las sesiones ONNX en memoria."""
    with _rvc_lock:
        _sessions.clear()
