import os
import re
import sys
import json
import time
import queue
import base64
import ctypes
import hashlib
import asyncio
import datetime
import threading
import subprocess
import webbrowser
from collections import deque

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
_BASE_DIR_INIT = os.path.dirname(os.path.abspath(__file__))
LOCAL_DATA_DIR = os.path.join(os.getenv("LOCALAPPDATA") or _BASE_DIR_INIT, "JARVIS")
os.makedirs(LOCAL_DATA_DIR, exist_ok=True)
_ES_HEADLESS = sys.stdout is None or sys.stderr is None
if _ES_HEADLESS:
    _log_path = os.path.join(LOCAL_DATA_DIR, "jarvis_headless.log")
    try:
        if os.path.exists(_log_path) and os.path.getsize(_log_path) > 262144:
            os.remove(_log_path)
    except OSError:
        pass
    _log_fh = open(_log_path, "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = _log_fh
    if sys.stderr is None:
        sys.stderr = _log_fh
try:
    import winsound
except ImportError:
    winsound = None
import warnings
with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import audioop
from urllib import request as urlrequest
from contextlib import asynccontextmanager
import psutil
import requests
import speech_recognition as sr
import uvicorn
from dotenv import load_dotenv
from google import genai
from google.genai import types
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

_pygame_mod = None


def _obtener_pygame():
    """Inicializa pygame.mixer solo bajo demanda para no dejar un hilo SDL activo en segundo plano."""
    global _pygame_mod
    if _pygame_mod is not None:
        return _pygame_mod or None
    try:
        import pygame
        _pygame_mod = pygame
        return pygame
    except Exception:
        _pygame_mod = False
        return None


_rvc_mod = None


def _rvc_tiene_modelos_en_disco() -> bool:
    models_dir = os.path.join(_BASE_DIR_INIT, "rvc_voice", "models")
    modelo_rvc = (os.getenv("RVC_MODEL_PATH") or os.path.join(models_dir, "jarvis_rvc.onnx")).strip()
    if not os.path.isfile(modelo_rvc):
        return False
    vec_env = (os.getenv("RVC_VEC_PATH") or "").strip()
    if vec_env:
        return os.path.isfile(vec_env)
    return (
        os.path.isfile(os.path.join(models_dir, "vec-768-layer-12.onnx"))
        or os.path.isfile(os.path.join(models_dir, "vec-256-layer-9.onnx"))
    )


def rvc_disponible() -> bool:
    """Verifica presencia de modelos RVC sin importar onnxruntime/pyworld si la carpeta está vacía."""
    return _rvc_tiene_modelos_en_disco()


def convertir_audio_rvc(wav_entrada: str, wav_salida: str) -> bool:
    global _rvc_mod
    if not _rvc_tiene_modelos_en_disco():
        return False
    if _rvc_mod is None:
        import rvc_onnx
        _rvc_mod = rvc_onnx
    return _rvc_mod.convertir_audio_rvc(wav_entrada, wav_salida)


def cerrar_rvc():
    if _rvc_mod is not None:
        _rvc_mod.cerrar_rvc()


from tools_windows import (
    obtener_estado_hardware,
    lanzar_aplicacion,
    cerrar_aplicacion,
    listar_aplicaciones_escritorio,
    aplicacion_disponible,
    precalentar_apps_instaladas,
    abrir_carpeta_explorador,
    abrir_sitio_web,
    buscar_en_google,
    revisar_gmail,
    bloquear_estacion_trabajo,
    silenciar_volumen,
    listar_archivos,
    DECLARACIONES_HERRAMIENTAS,
    MAPA_HERRAMIENTAS,
)
from tools_multimedia import ajustar_volumen, ajustar_volumen_delta, controlar_reproduccion
from tools_spotify import reproducir_musica, obtener_cancion_actual, ajustar_volumen_spotify, listar_playlists_spotify
from tts_piper import piper_disponible, sintetizar_piper, cerrar_piper
from jarvis_persona import (
    FRASES_PRECALENTAMIENTO,
    SYSTEM_INSTRUCTION,
    elegir_saludo_wake,
    respuesta_oral_tool as _respuesta_oral_tool,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
TTS_CACHE_DIR = os.path.join(LOCAL_DATA_DIR, "tts_cache")
os.makedirs(TTS_CACHE_DIR, exist_ok=True)

_env_mtime: float | None = None


def _cargar_env_si_cambio():
    """Carga .env solo cuando el archivo cambia en disco para evitar I/O repetido."""
    global _env_mtime
    try:
        mtime = os.path.getmtime(ENV_PATH)
    except OSError:
        mtime = None
    if mtime is not None and mtime != _env_mtime:
        load_dotenv(ENV_PATH, override=True)
        _env_mtime = mtime


_cargar_env_si_cambio()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")  # Opcional: inferencia LPU ultrarrápida (~0.2s)

if not GEMINI_API_KEY:
    raise ValueError("No se encontró GEMINI_API_KEY en el archivo .env")

client = genai.Client(api_key=GEMINI_API_KEY)
try:
    _pool_gemini = getattr(getattr(getattr(client, "_api_client", None), "_httpx_client", None), "_transport", None)
    _pool_gemini = getattr(_pool_gemini, "_pool", None)
    if _pool_gemini is not None and hasattr(_pool_gemini, "_keepalive_expiry"):
        _pool_gemini._keepalive_expiry = 120.0
except Exception:
    pass
MODELO_GEMINI = "gemini-3.5-flash-lite"
MODELO_GROQ = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

_GEMINI_FUNCTION_DECLS = [
    types.FunctionDeclaration(
        name=d["name"],
        description=d["description"],
        parameters_json_schema=d["parameters"],
    )
    for d in DECLARACIONES_HERRAMIENTAS
]
_GEMINI_CONFIG = types.GenerateContentConfig(
    system_instruction=SYSTEM_INSTRUCTION,
    tools=[types.Tool(function_declarations=_GEMINI_FUNCTION_DECLS)],
    thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL),
    max_output_tokens=160,
)
_CHAT_HISTORIAL: deque[types.Content] = deque(maxlen=6)
gemini_lock = threading.Lock()
_ocupado_comando = threading.Event()

# Variantes fonéticas con las que Google STT transcribe "Jarvis" en español rioplatense
WAKE_WORDS_FUERTES = (
    "jarvis", "harvis", "sharvis", "charvis", "yarvis", "garvis", "darvis", "arvis",
    "jarvi", "harvi", "sharvi", "charvi", "yarvi", "arvi",
    "jarbi", "harbi", "sharbi", "charbi", "yarbi", "arbi",
    "jarbis", "harbis", "sharbis", "charbis", "yarbis", "arbis",
    "harvey", "jarvey", "harvy", "jarvy", "yarvey",
    "yavis", "javis", "havis", "shavis", "chavis",
    "jaris", "haris", "yaris", "sharis", "sarvis", "sarvi",
    "jarv", "harv", "jarb", "harb",
)
WAKE_WORDS_DEBILES = (
    "service", "servis", "sarvia",
    "charlie", "charly", "harry",
    "xavi", "shabi", "chabi", "yabi", "jabi", "llavis", "shavi", "chavi", "javi",
    "shari", "chari", "saris",
    "jar", "har", "shar", "yar",
)
WAKE_WORDS = WAKE_WORDS_FUERTES + WAKE_WORDS_DEBILES
WAKE_WORD_FUERTE_REGEX = re.compile(
    r"\b(?:"
    r"(?:j|h|y|sh|ch|g|d|ll|x)?(?:ar|er)(?:v|b)(?:is|i|ey|y|es|e)?"
    r"|(?:y|j|h|sh|ch|x|ll)avis"
    r"|(?:j|h|y|sh)arr?is"
    r"|sarvi(?:s)?"
    r")\b",
    re.IGNORECASE,
)
WAKE_WORD_DEBIL_REGEX = re.compile(
    r"\b(?:"
    r"s(?:e|a)rvi(?:ce|s|a)"
    r"|charl(?:ie|y)|harry"
    r"|(?:y|j|h|sh|ch|x|ll)a(?:v|b)i(?:s)?"
    r"|(?:sh|ch)ari|saris"
    r"|jar|har|shar|yar"
    r")\b",
    re.IGNORECASE,
)
_PREFIJOS_SALUDO_WAKE = {
    "che", "hey", "ey", "eh", "hola", "oye", "oi", "oí", "bueno",
    "ok", "okay", "okei", "a", "ver", "mira", "mirá", "escucha", "escuchá",
    "por", "favor", "dale", "disculpa", "perdón", "perdon",
}


class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in list(self.active_connections):
            try:
                await connection.send_json(message)
            except Exception:
                self.disconnect(connection)


manager = ConnectionManager()
loop_principal: asyncio.AbstractEventLoop | None = None


def emitir_evento_sync(tipo: str, payload: dict):
    """Permite al hilo de voz enviar mensajes WebSocket de forma segura."""
    if loop_principal and loop_principal.is_running():
        mensaje = {"type": tipo, **payload}
        asyncio.run_coroutine_threadsafe(manager.broadcast(mensaje), loop_principal)


def _unificar_respuestas_tools(respuestas: list[str]) -> str:
    unicas: list[str] = []
    for r in respuestas:
        if r and r not in unicas:
            unicas.append(r)
    return " ".join(unicas)


def _registrar_tool_hud(nombre: str, args: dict, resultado: str) -> str:
    print(f"[Tool rápida] {nombre}({args}) -> {resultado}")
    emitir_evento_sync(
        "tool_call",
        {
            "tool": nombre,
            "args": args,
            "result": resultado,
        },
    )
    return _respuesta_oral_tool(nombre, resultado)


def _intentar_ejecucion_instantanea(comando: str) -> str | None:
    """Ejecuta comandos directos frecuentes en <50ms sin esperar latencia de red del LLM."""
    texto = comando.lower().strip()

    # 0. Control de volumen exclusivo de la música / Spotify (sin tocar el volumen maestro de Windows)
    if any(k in texto for k in ("música", "musica", "spotify", "canción", "cancion", "tema")):
        m_vol_mus = re.search(r"(?:a\s+un|al|a|en)\s+(\d{1,3})\s*%?\b", texto)
        if m_vol_mus and any(v in texto for v in ("volumen", "baja", "bajá", "subi", "subí", "pone", "poné", "deja", "dejá")):
            args = {"porcentaje": int(m_vol_mus.group(1))}
            return _registrar_tool_hud("ajustar_volumen_spotify", args, ajustar_volumen_spotify(**args))
        m_delta_mus = re.search(r"(?:sub[ií]|baj[aá]|bajen|bajan)\s+(?:el\s+volumen\s+(?:de\s+la\s+m[uú]sica\s+)?)?(?:un\s+)?(\d{1,3})\s*%", texto)
        if m_delta_mus:
            val = int(m_delta_mus.group(1))
            delta = -val if any(b in texto for b in ("baj", "menos")) else val
            args = {"delta": delta}
            return _registrar_tool_hud("ajustar_volumen_spotify", args, ajustar_volumen_spotify(**args))
        if any(b in texto for b in ("baja", "bajá", "bajar", "bajan", "menos volumen", "más bajo", "mas bajo", "suave")):
            args = {"delta": -25}
            return _registrar_tool_hud("ajustar_volumen_spotify", args, ajustar_volumen_spotify(**args))
        if any(s in texto for s in ("subi", "subí", "subir", "suben", "más volumen", "mas volumen", "más fuerte", "mas fuerte")):
            args = {"delta": 25}
            return _registrar_tool_hud("ajustar_volumen_spotify", args, ajustar_volumen_spotify(**args))

    # 1. Atajos directos de biblioteca personal y radio de Spotify (sin latencia LLM)
    if re.search(r"\b(?:mis\s+me\s+gustas?|mis\s+favorit[ao]s|lo\s+que\s+m[aá]s\s+escucho|lo\s+[uú]ltimo\s+que\s+escuch[eé]|mix\s+aleatorio)\b", texto):
        args = {"busqueda": texto, "tipo": "playlist"}
        return _registrar_tool_hud("reproducir_musica", args, reproducir_musica(**args))
    if re.search(r"^(?:pon[eé]|reproduc[ií]|escuchar|dale\s+a)?\s*(?:la\s+|una\s+)?mi\s+radio\b", texto):
        args = {"busqueda": "mi radio", "tipo": "radio"}
        return _registrar_tool_hud("reproducir_musica", args, reproducir_musica(**args))

    # 2. Pausar / reanudar / detener / siguiente / anterior
    if texto in ("pausa", "pausar", "pausá", "pausá la música", "pausa la musica", "pausame la música", "pausá esa", "pausa esa", "poné pausa", "pone pausa"):
        args = {"accion": "pausa"}
        return _registrar_tool_hud("controlar_reproduccion", args, controlar_reproduccion(**args))
    if texto in ("reanudar", "continuar", "play", "reproducir", "dale play", "poné play", "pone play", "seguí la música", "segui la musica"):
        args = {"accion": "play"}
        return _registrar_tool_hud("controlar_reproduccion", args, controlar_reproduccion(**args))
    if any(p in texto for p in ("deten", "pará la música", "para la musica", "frená la música", "frena la musica", "stop")):
        args = {"accion": "detener"}
        return _registrar_tool_hud("controlar_reproduccion", args, controlar_reproduccion(**args))
    if texto in ("siguiente", "pasá", "pasa", "cambiá de tema", "cambia de tema", "otro tema") or any(
        p in texto for p in ("siguiente tema", "siguiente canción", "siguiente cancion", "pasá de tema", "pasa de tema")
    ):
        args = {"accion": "siguiente"}
        return _registrar_tool_hud("controlar_reproduccion", args, controlar_reproduccion(**args))
    if texto in ("anterior", "volvé", "volve") or any(
        p in texto for p in ("tema anterior", "canción anterior", "cancion anterior", "volvé al tema")
    ):
        args = {"accion": "anterior"}
        return _registrar_tool_hud("controlar_reproduccion", args, controlar_reproduccion(**args))

    # 3. Volumen porcentual y relativo maestro ("volumen al 50", "subí el volumen", "bajá el volumen un 20%")
    m_vol = re.search(r"volumen\s+(?:a\s+un|al|a|en)\s+(\d{1,3})\s*%?", texto)
    if m_vol:
        nivel = int(m_vol.group(1))
        args = {"porcentaje": nivel}
        return _registrar_tool_hud("ajustar_volumen", args, ajustar_volumen(**args))

    if "volumen" in texto:
        m_delta_gen = re.search(r"(?:sub[ií]|baj[aá])\s+(?:el\s+volumen\s+)?(?:un\s+)?(\d{1,3})\s*%", texto)
        if m_delta_gen:
            val = int(m_delta_gen.group(1))
            delta = -val if "baj" in texto else val
            return _registrar_tool_hud("ajustar_volumen", {"delta": delta}, ajustar_volumen_delta(delta))
        if re.search(r"\b(?:baj[aá]|bajar|menos)\b", texto):
            return _registrar_tool_hud("ajustar_volumen", {"delta": -15}, ajustar_volumen_delta(-15))
        if re.search(r"\b(?:sub[ií]|subir|m[aá]s)\b", texto):
            return _registrar_tool_hud("ajustar_volumen", {"delta": 15}, ajustar_volumen_delta(15))

    if any(p in texto for p in ("silenciá", "silencia", "mutear", "muteá", "desmutear")):
        return _registrar_tool_hud("silenciar_volumen", {}, silenciar_volumen())

    # 3b. Búsqueda rápida en Google (<5ms sin latencia LLM)
    m_google = re.search(
        r"^(?:che\s+|por\s+favor\s+)?(?:me\s+)?(?:pod[eé]s\s+)?(?:busc[aá]|buscar|buscame|google[aá]|googlear)\s+(.+?)(?:\s+en\s+google)?[?.!]*$",
        texto,
    )
    if m_google:
        consulta_g = m_google.group(1).strip()
        if consulta_g and not re.search(r"\b(?:archivo|archivos|carpeta|carpetas|escritorio|descargas|documentos|gmail|correo|mail)\b", consulta_g):
            args = {"consulta": consulta_g}
            return _registrar_tool_hud("buscar_en_google", args, buscar_en_google(**args))

    # 4. Estado del hardware / diagnóstico
    if any(p in texto for p in ("estado del hardware", "uso de cpu", "estado del sistema", "diagnóstico", "diagnostico")):
        return _registrar_tool_hud("obtener_estado_hardware", {}, obtener_estado_hardware())

    # 5. Revisar Gmail rápido (antes de abrir sitio web)
    if re.search(
        r"\b(?:revis[aá]|cheque[aá]|le[eé]|mir[aá]|fijate(?:\s+en)?)\s+(?:el\s+|mi\s+)?(?:gmail|correo|correos|mail|mails)\b"
        r"|\b(?:tengo|hay)\s+(?:correos|mails|mensajes\s+en\s+gmail)\b",
        texto,
    ):
        _cargar_env_si_cambio()
        return _registrar_tool_hud("revisar_gmail", {}, revisar_gmail())

    # 6. Abrir sitios web conocidos
    m_web = re.search(
        r"\b(?:abr[ií]|abre|abrir|entr[aá]\s+a|anda\s+a)\s+(?:el\s+|la\s+)?(gmail|youtube|whatsapp\s+web|drive|google\s+drive|chatgpt|github)\b",
        texto,
    )
    if m_web:
        args = {"destino": m_web.group(1).strip()}
        return _registrar_tool_hud("abrir_sitio_web", args, abrir_sitio_web(**args))

    # 7. Abrir y cerrar aplicaciones de escritorio, Menú Inicio, Store/MSIX o carpetas conocidas
    es_pedido_web = bool(
        re.search(r"\b(?:en\s+google|en\s+el\s+navegador|p[aá]gina|sitio|web)\b|\.[a-z]{2,4}\b|https?://", texto)
    )
    if not es_pedido_web:
        m_abrir = re.search(
            r"^(?:che\s+|jarvis\s+)?(?:por\s+favor\s+)?(?:me\s+|nos\s+)?(?:pod[eé]s\s+|puedes\s+|quiero\s+)?"
            r"(?:abr(?:[ií]s?|[ií]me|ime|ir|e)|lanz(?:[aá]s?|[aá]me|ame|ar)|inici(?:[aá]s?|[aá]me|ame|ar)|ejecut(?:[aá]s?|[aá]me|ame|ar))"
            r"\s+(?:el\s+programa\s+|la\s+aplicaci[oó]n\s+|la\s+app\s+|la\s+carpeta\s+(?:de\s+)?|el\s+|la\s+)?(.+?)[?.!]*$",
            texto,
        )
        if m_abrir:
            objetivo = m_abrir.group(1).strip()
            objetivo_limpio = re.sub(r"\s+(?:de|del|en\s+el)\s+escritorio$", "", objetivo).strip()
            if objetivo_limpio in ("escritorio", "descargas", "downloads", "documentos", "documents", "jarvis", "carpeta jarvis"):
                args = {"ubicacion": objetivo_limpio}
                return _registrar_tool_hud("abrir_carpeta_explorador", args, abrir_carpeta_explorador(**args))
            if aplicacion_disponible(objetivo):
                args = {"nombre": objetivo}
                return _registrar_tool_hud("lanzar_aplicacion", args, lanzar_aplicacion(**args))

        m_cerrar = re.search(
            r"^(?:che\s+|jarvis\s+)?(?:por\s+favor\s+)?(?:me\s+)?(?:pod[eé]s\s+|puedes\s+)?"
            r"(?:cerr(?:[aá]s?|[aá]me|ame|ar)|cierr(?:a|e)|mat(?:[aá]|ar))"
            r"\s+(?:el\s+programa\s+|la\s+aplicaci[oó]n\s+|la\s+app\s+|el\s+|la\s+)?(.+?)[?.!]*$",
            texto,
        )
        if m_cerrar:
            objetivo = m_cerrar.group(1).strip()
            if aplicacion_disponible(objetivo):
                args = {"nombre": objetivo}
                return _registrar_tool_hud("cerrar_aplicacion", args, cerrar_aplicacion(**args))

    # 8. Canción actual y playlists del usuario
    if any(p in texto for p in ("qué está sonando", "que esta sonando", "qué canción suena", "que cancion suena")):
        return _registrar_tool_hud("obtener_cancion_actual", {}, obtener_cancion_actual())
    if re.search(r"\b(?:qu[eé]\s+playlists|mis\s+playlists|list[aá]\s+(?:mis\s+)?playlists|ver\s+mis\s+playlists)\b", texto):
        return _registrar_tool_hud("listar_playlists_spotify", {}, listar_playlists_spotify())

    # 9. Bloquear estación de trabajo
    if re.search(r"\bbloque(?:á|a|ar)\s+(?:la\s+)?(?:pantalla|pc|compu|computadora|estaci[oó]n|sesi[oó]n)\b", texto):
        return _registrar_tool_hud("bloquear_estacion_trabajo", {}, bloquear_estacion_trabajo())

    # 10. Hora y fecha actual local (sin gastar tokens ni red)
    if any(p in texto for p in ("qué hora es", "que hora es", "decime la hora", "dime la hora", "la hora")):
        ahora = datetime.datetime.now().strftime("%H:%M")
        return f"Son las {ahora}, jefe."
    if any(p in texto for p in ("qué día es", "que dia es", "fecha de hoy", "qué fecha es", "que fecha es")):
        ahora = datetime.datetime.now().strftime("%d/%m/%Y")
        return f"Hoy es {ahora}."

    # 11. Listar aplicaciones del escritorio o archivos de carpetas conocidas
    if re.search(r"\b(?:qu[eé]|que|list[aá]|mostr[aá])\s+(?:aplicaciones|apps|programas|juegos)\b.*\bescritorio\b", texto):
        return _registrar_tool_hud("listar_aplicaciones_escritorio", {}, listar_aplicaciones_escritorio())
    m_list = re.search(r"\b(?:list(?:á|a|ar)|mostr(?:á|a|ar)|qué hay en|que hay en)\s+(?:los\s+archivos\s+(?:de|del|en)\s+)?(?:el\s+|la\s+)?(escritorio|descargas|documentos|jarvis)\b", texto)
    if m_list:
        args = {"ubicacion": m_list.group(1)}
        return _registrar_tool_hud("listar_archivos", args, listar_archivos(**args))

    return None


_PATRON_MUSICA_AUDIO = re.compile(
    r"\b(?:pon(?:e|é|eme|éme|er)?|reproduc(?:i|í|ir|e|ime|íme|zca|zcas)?|escuch(?:a|á|ar|ame|áme)?|toc(?:a|á|ar)|pas(?:a|á|ame|áme)|m[uú]sica|musica|canci[oó]n|cancion|tema|artista|[aá]lbum|album|playlist|playlists|lista|listas|radio|radios|mix|me\s+gusta|favorit[ao]s|spotify)\b",
    re.IGNORECASE,
)


def _requiere_audio_multimodal(comando: str) -> bool:
    """Adjunta audio WAV a Gemini solo para música/nombres en inglés o cuando STT falló, ahorrando tokens y ancho de banda."""
    texto = (comando or "").strip()
    return texto == "(orden de voz)" or bool(_PATRON_MUSICA_AUDIO.search(texto))


def _consultar_groq_ultrarrapido(comando: str) -> str | None:
    """Si GROQ_API_KEY está configurada en .env, resuelve en ~0.2s usando LPU."""
    if not GROQ_API_KEY:
        return None
    try:
        tools_openai = [
            {
                "type": "function",
                "function": {
                    "name": d["name"],
                    "description": d["description"],
                    "parameters": d["parameters"],
                },
            }
            for d in DECLARACIONES_HERRAMIENTAS
        ]
        payload = json.dumps(
            {
                "model": MODELO_GROQ,
                "messages": [
                    {"role": "system", "content": SYSTEM_INSTRUCTION},
                    {"role": "user", "content": comando},
                ],
                "tools": tools_openai,
                "tool_choice": "auto",
                "temperature": 0.75,
                "max_tokens": 220,
            }
        ).encode("utf-8")
        req = urlrequest.Request(
            "https://api.groq.com/openai/v1/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
                "User-Agent": "JARVIS-HUD/2.0",
            },
            method="POST",
        )
        with urlrequest.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        msg = data["choices"][0]["message"]
        tool_calls = msg.get("tool_calls") or []
        if tool_calls:
            resultados = []
            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                args = json.loads(tc["function"].get("arguments") or "{}")
                fn = MAPA_HERRAMIENTAS.get(fn_name)
                res = fn(**args) if fn else f"Herramienta desconocida: {fn_name}"
                resultados.append(_registrar_tool_hud(fn_name, args, res))
            return _unificar_respuestas_tools(resultados)
        return (msg.get("content") or "").strip()
    except Exception as e:
        print(f"[Aviso Groq fallback a Gemini]: {e}")
        return None


def _registrar_turno_historial(usuario_txt: str, modelo_txt: str):
    u = (usuario_txt or "").strip()
    m = (modelo_txt or "").strip()
    if u and m:
        _CHAT_HISTORIAL.append(types.Content(role="user", parts=[types.Part.from_text(text=u)]))
        _CHAT_HISTORIAL.append(types.Content(role="model", parts=[types.Part.from_text(text=m)]))


def _extraer_oraciones_listas(buffer: str) -> tuple[list[str], str]:
    """Separa oraciones completas listas para hablar sin esperar el resto de la respuesta."""
    oraciones: list[str] = []
    while True:
        m = re.search(r"[.!?\n]+(?:\s+|$)", buffer)
        if not m:
            break
        corte = m.end()
        trozo = buffer[:corte].strip()
        resto = buffer[corte:]
        if len(trozo) >= 18 or not resto:
            if trozo:
                oraciones.append(trozo)
            buffer = resto
        else:
            # Si la oración es muy corta (ej. "Dale."), combinarla con la siguiente salvo fin de texto
            siguiente = re.search(r"[.!?\n]+(?:\s+|$)", resto)
            if not siguiente:
                break
            corte2 = corte + siguiente.end()
            trozo2 = buffer[:corte2].strip()
            if trozo2:
                oraciones.append(trozo2)
            buffer = buffer[corte2:]
    return oraciones, buffer


def consultar_gemini_con_tools(
    comando: str,
    audio_wav_bytes: bytes | None = None,
    on_sentence=None,
) -> tuple[str, bool]:
    """1) Fast-path local (<50ms), 2) Groq LPU (~200ms), 3) Gemini generate_content_stream (~0.75s) con memoria."""
    t0 = time.perf_counter()
    respuesta_rapida = _intentar_ejecucion_instantanea(comando)
    if respuesta_rapida is not None:
        print(f"[Latencia Fast-Path Local]: {(time.perf_counter()-t0)*1000:.0f} ms")
        _registrar_turno_historial(comando, respuesta_rapida)
        return respuesta_rapida, False

    if audio_wav_bytes is None:
        respuesta_groq = _consultar_groq_ultrarrapido(comando)
        if respuesta_groq is not None:
            print(f"[Latencia Groq LPU]: {(time.perf_counter()-t0)*1000:.0f} ms")
            _registrar_turno_historial(comando, respuesta_groq)
            return respuesta_groq, False

    if audio_wav_bytes:
        prompt_audio = (
            f"Escucha el audio original con prioridad (el STT aproximado fue: '{comando}'). "
            "Ignora la wake word si aparece. El STT en español suele deformar nombres de canciones o artistas en inglés "
            "(ej. 'swiss dream' por 'Sweet Dreams', 'confidence' por 'Confident'), así que corregilos al nombre real según el audio. "
            "Si el audio está cortado, es ambiguo o solo tiene 1 o 2 letras sin sentido musical claro, NO adivines un tema cualquiera: pedí aclaración brevemente."
        )
        contenido_actual = types.Content(
            role="user",
            parts=[
                types.Part.from_bytes(data=audio_wav_bytes, mime_type="audio/wav"),
                types.Part.from_text(text=prompt_audio),
            ],
        )
    else:
        contenido_actual = types.Content(
            role="user",
            parts=[types.Part.from_text(text=comando)],
        )

    with gemini_lock:
        contenidos = list(_CHAT_HISTORIAL) + [contenido_actual]
        tool_calls = []
        buffer_texto = ""
        partes_texto: list[str] = []
        emitio_stream_oral = False

        for chunk in client.models.generate_content_stream(
            model=MODELO_GEMINI,
            contents=contenidos,
            config=_GEMINI_CONFIG,
        ):
            if chunk.function_calls:
                tool_calls.extend(chunk.function_calls)
            if chunk.text:
                partes_texto.append(chunk.text)
                if on_sentence is not None and not tool_calls:
                    buffer_texto += chunk.text
                    oraciones, buffer_texto = _extraer_oraciones_listas(buffer_texto)
                    for oracion in oraciones:
                        emitio_stream_oral = True
                        on_sentence(oracion)

        if tool_calls:
            respuestas_tools = []
            resumenes_memoria = []
            for fc in tool_calls:
                args = dict(fc.args or {})
                fn = MAPA_HERRAMIENTAS.get(fc.name)
                resultado = fn(**args) if fn else f"Herramienta desconocida: {fc.name}"
                respuestas_tools.append(_registrar_tool_hud(fc.name, args, resultado))
                resumenes_memoria.append(resultado)

            respuesta_final = _unificar_respuestas_tools(respuestas_tools)
            _registrar_turno_historial(comando, " ".join(resumenes_memoria) or respuesta_final)
            print(f"[Latencia Gemini Stream+Tools]: {(time.perf_counter()-t0)*1000:.0f} ms")
            return respuesta_final, False

        if on_sentence is not None and buffer_texto.strip():
            emitio_stream_oral = True
            on_sentence(buffer_texto.strip())

        texto_final = "".join(partes_texto).strip()
        _registrar_turno_historial(comando, texto_final)
        print(f"[Latencia Gemini Stream]: {(time.perf_counter()-t0)*1000:.0f} ms")
        return texto_final, emitio_stream_oral


# --- SÍNTESIS DE VOZ NEURONAL CON PROCESADOR ACÚSTICO DE CASCO (MARK III HUD DSP) ---
import wave

JARVIS_VOICE = os.getenv("JARVIS_VOICE", "es-CO-GonzaloNeural")
JARVIS_RATE = os.getenv("JARVIS_RATE", "+8%")
JARVIS_PITCH = os.getenv("JARVIS_PITCH", "-5Hz")

# Cadena DSP inspirada en el diseño sonoro de Skywalker Sound para J.A.R.V.I.S. en Iron Man:
# - Recorte de subgraves + cuerpo cálido en 240Hz
# - Realce táctico de presencia en 3.2kHz
# - Micro-doblaje armónico de fase a 9ms (timbre holográfico de casco)
# - Compresión dinámica de cabina
JARVIS_HUD_FILTER = (
    "highpass=f=130,"
    "equalizer=f=240:width_type=q:width=1.2:g=2.5,"
    "equalizer=f=3200:width_type=q:width=1.4:g=4.2,"
    "aecho=0.85:0.88:9:0.22,"
    "acompressor=threshold=-18dB:ratio=3.5:attack=4:release=45:makeup=2"
)


def _ruta_cache_tts(texto: str, es_clonada: bool = False) -> str:
    _cargar_env_si_cambio()
    vid = (os.getenv("JARVIS_VOICE_ID") or "").strip()
    tag = vid[:10] if (es_clonada and vid) else f"{JARVIS_VOICE}:{JARVIS_PITCH}"
    clave = hashlib.sha1(f"{tag}:{texto.strip().lower()}".encode("utf-8")).hexdigest()[:16]
    prefijo = "clone" if (es_clonada and vid) else "hud"
    return os.path.join(TTS_CACHE_DIR, f"{prefijo}_{clave}.wav")


_cached_fish_voice_id: str | None = None
_fish_lookup_attempted = False


def _resolver_credenciales_voz() -> tuple[str, str]:
    """Obtiene (api_key, voice_id) desde .env, o consulta el Agente de Fish Audio una sola vez."""
    global _cached_fish_voice_id, _fish_lookup_attempted
    _cargar_env_si_cambio()
    api_key = (os.getenv("FISH_API_KEY") or os.getenv("JARVIS_VOICE_API_KEY") or "").strip()
    voice_id = (os.getenv("JARVIS_VOICE_ID") or _cached_fish_voice_id or "").strip()
    agent_id = (os.getenv("FISH_AGENT_ID") or "7e8e33caac214c3996800490800910e0").strip()

    if api_key and not voice_id and agent_id and not _fish_lookup_attempted:
        _fish_lookup_attempted = True
        for url in (
            f"https://api.fish.audio/v1/agent/{agent_id}",
            f"https://api.fish.audio/v1/agents/{agent_id}",
        ):
            try:
                req = urlrequest.Request(
                    url,
                    headers={"Authorization": f"Bearer {api_key}", "User-Agent": "JARVIS-HUD/2.0"},
                )
                with urlrequest.urlopen(req, timeout=5) as resp:
                    info = json.loads(resp.read().decode("utf-8"))
                vid = (
                    info.get("voice_id")
                    or info.get("reference_id")
                    or (info.get("voice") or {}).get("id")
                    or (info.get("tts") or {}).get("reference_id")
                )
                if vid:
                    _cached_fish_voice_id = str(vid).strip()
                    return api_key, _cached_fish_voice_id
            except Exception:
                pass
    return api_key, voice_id


_fish_tts_sin_saldo = False


def _sintetizar_voz_clonada(texto: str, archivo_wav: str, reproducir_en_vivo: bool = True) -> bool:
    """Si FISH_API_KEY / JARVIS_VOICE_API_KEY está en .env, transmite la voz clonada (Fish Audio o ElevenLabs)."""
    global _fish_tts_sin_saldo
    if _fish_tts_sin_saldo:
        return False
    api_key, voice_id = _resolver_credenciales_voz()
    if not api_key or not voice_id:
        return False

    # Auto-detección de proveedor (Fish Audio usa keys sk-fish-... o IDs hexadecimales de 32 caracteres)
    es_fish = api_key.startswith("sk-fish-") or (bool(re.fullmatch(r"[0-9a-fA-F]{32}", voice_id)) and not api_key.startswith("sk_"))

    try:
        import pyaudio

        if es_fish:
            payload = json.dumps(
                {
                    "text": texto,
                    "reference_id": voice_id,
                    "format": "pcm",
                    "sample_rate": 24000,
                    "latency": "balanced",
                }
            ).encode("utf-8")
            req = urlrequest.Request(
                "https://api.fish.audio/v1/tts",
                data=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "JARVIS-HUD/2.0",
                },
                method="POST",
            )
        else:
            payload = json.dumps(
                {
                    "text": texto,
                    "model_id": "eleven_flash_v2_5",
                    "voice_settings": {
                        "stability": 0.65,
                        "similarity_boost": 0.85,
                        "style": 0.15,
                        "use_speaker_boost": True,
                    },
                }
            ).encode("utf-8")
            req = urlrequest.Request(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream?output_format=pcm_24000&optimize_streaming_latency=3",
                data=payload,
                headers={
                    "xi-api-key": api_key,
                    "Content-Type": "application/json",
                    "User-Agent": "JARVIS-HUD/2.0",
                },
                method="POST",
            )

        pcm_buffers: list[bytes] = []
        with urlrequest.urlopen(req, timeout=10) as resp:
            pa = pyaudio.PyAudio() if reproducir_en_vivo else None
            out_stream = (
                pa.open(format=pyaudio.paInt16, channels=1, rate=24000, output=True)
                if pa is not None
                else None
            )
            try:
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    pcm_buffers.append(chunk)
                    if out_stream is not None:
                        out_stream.write(chunk)
            finally:
                if out_stream is not None:
                    out_stream.stop_stream()
                    out_stream.close()
                if pa is not None:
                    pa.terminate()

        if pcm_buffers:
            with wave.open(archivo_wav, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(24000)
                wf.writeframes(b"".join(pcm_buffers))
            return True
    except Exception as e:
        if "402" in str(e):
            _fish_tts_sin_saldo = True
            print("[Info Fish Audio]: Endpoint /v1/tts requiere saldo API; usando sesión de Agente LiveKit en el HUD y voz táctica local en consola.")
        else:
            print(f"[Aviso Voz Clonada]: {e}")
    return False


def _reproducir_audio_local(archivo: str):
    if archivo.endswith(".wav") and winsound is not None:
        try:
            winsound.PlaySound(archivo, winsound.SND_FILENAME)
            return
        except Exception:
            pass
    pg = _obtener_pygame()
    if pg is not None:
        try:
            if not pg.mixer.get_init():
                pg.mixer.init()
            pg.mixer.music.load(archivo)
            pg.mixer.music.play()
            while pg.mixer.music.get_busy():
                pg.time.Clock().tick(30)
            pg.mixer.music.unload()
            return
        except Exception:
            pass
    mci = ctypes.windll.winmm.mciSendStringW
    mci(f'open "{archivo}" type mpegvideo alias jarvis_srv_voice', None, 0, None)
    mci("play jarvis_srv_voice wait", None, 0, None)
    mci("close jarvis_srv_voice", None, 0, None)


def _generar_wav_hud(texto: str, archivo_wav: str, reproducir_en_vivo: bool = True) -> bool:
    """Genera y opcionalmente reproduce en vivo la voz con el filtro acústico de J.A.R.V.I.S."""
    try:
        import imageio_ffmpeg
        import pyaudio

        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        proc = subprocess.Popen(
            [
                ffmpeg_exe,
                "-fflags", "nobuffer",
                "-flags", "low_delay",
                "-i", "pipe:0",
                "-af", JARVIS_HUD_FILTER,
                "-f", "s16le",
                "-acodec", "pcm_s16le",
                "-ar", "24000",
                "-ac", "1",
                "-loglevel", "quiet",
                "pipe:1",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )

        pa = pyaudio.PyAudio() if reproducir_en_vivo else None
        out_stream = (
            pa.open(format=pyaudio.paInt16, channels=1, rate=24000, output=True)
            if pa is not None
            else None
        )
        pcm_buffers: list[bytes] = []

        def _lector_pcm():
            try:
                while True:
                    pcm = proc.stdout.read(2048)
                    if not pcm:
                        break
                    pcm_buffers.append(pcm)
                    if out_stream is not None:
                        out_stream.write(pcm)
            except Exception:
                pass

        hilo_audio = threading.Thread(target=_lector_pcm, daemon=True)
        hilo_audio.start()

        async def _stream_tts():
            import edge_tts

            comm = edge_tts.Communicate(texto, JARVIS_VOICE, rate=JARVIS_RATE, pitch=JARVIS_PITCH)
            async for chunk in comm.stream():
                if chunk["type"] == "audio" and proc.stdin:
                    try:
                        proc.stdin.write(chunk["data"])
                        proc.stdin.flush()
                    except Exception:
                        pass
            if proc.stdin:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        asyncio.run(_stream_tts())
        hilo_audio.join(timeout=15)
        if out_stream is not None:
            out_stream.stop_stream()
            out_stream.close()
        if pa is not None:
            pa.terminate()
        proc.wait(timeout=2)

        if pcm_buffers:
            with wave.open(archivo_wav, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(24000)
                wf.writeframes(b"".join(pcm_buffers))
            return True
    except Exception as e:
        print(f"[Error DSP HUD]: {e}")
    return False


def _ruta_cache_piper(texto: str) -> str:
    modelo = (os.getenv("PIPER_MODEL") or "es_MX-claude-14947-epoch-high.onnx").strip()
    scale = (os.getenv("PIPER_LENGTH_SCALE") or "0.90").strip()
    dsp = (os.getenv("JARVIS_VOICE_DSP") or "1").strip()
    clave = hashlib.sha1(f"piper_v2:{os.path.basename(modelo)}:{scale}:{dsp}:{texto.strip().lower()}".encode("utf-8")).hexdigest()[:16]
    return os.path.join(TTS_CACHE_DIR, f"piper_{clave}.wav")


def _ruta_cache_rvc(texto: str) -> str:
    modelo = (os.getenv("RVC_MODEL_PATH") or "jarvis_rvc.onnx").strip()
    f0_key = (os.getenv("RVC_F0_UP_KEY") or "0").strip()
    clave = hashlib.sha1(f"rvc:{os.path.basename(modelo)}:{f0_key}:{texto.strip().lower()}".encode("utf-8")).hexdigest()[:16]
    return os.path.join(TTS_CACHE_DIR, f"rvc_{clave}.wav")


def hablar_sync(texto: str):
    if not texto:
        emitir_evento_sync("status", {"state": "STANDBY"})
        return

    emitir_evento_sync("status", {"state": "SPEAKING"})
    _cargar_env_si_cambio()
    usar_nube = (os.getenv("JARVIS_TTS_ENGINE") or "piper").strip().lower() == "cloud"

    # 0. Motor local Piper + conversión de timbre RVC ONNX (si hay modelo RVC en rvc_voice/models/)
    if not usar_nube and piper_disponible():
        if rvc_disponible():
            archivo_rvc = _ruta_cache_rvc(texto)
            if os.path.exists(archivo_rvc) and os.path.getsize(archivo_rvc) > 256:
                _reproducir_audio_local(archivo_rvc)
                emitir_evento_sync("status", {"state": "STANDBY"})
                return
            tmp_piper = os.path.join(TTS_CACHE_DIR, f"_tmp_rvc_in_{threading.get_ident()}.wav")
            try:
                if sintetizar_piper(texto, tmp_piper):
                    if convertir_audio_rvc(tmp_piper, archivo_rvc):
                        _reproducir_audio_local(archivo_rvc)
                        emitir_evento_sync("status", {"state": "STANDBY"})
                        return
                    _reproducir_audio_local(tmp_piper)
                    emitir_evento_sync("status", {"state": "STANDBY"})
                    return
            finally:
                if os.path.exists(tmp_piper):
                    try:
                        os.remove(tmp_piper)
                    except OSError:
                        pass

        archivo_piper = _ruta_cache_piper(texto)
        if os.path.exists(archivo_piper) and os.path.getsize(archivo_piper) > 256:
            _reproducir_audio_local(archivo_piper)
            emitir_evento_sync("status", {"state": "STANDBY"})
            return
        if sintetizar_piper(texto, archivo_piper):
            _reproducir_audio_local(archivo_piper)
            emitir_evento_sync("status", {"state": "STANDBY"})
            return

    archivo_clonada = _ruta_cache_tts(texto, es_clonada=True)

    # 1. Si ya está en caché local la voz clonada, suena en ~5ms
    if os.path.exists(archivo_clonada) and os.path.getsize(archivo_clonada) > 256:
        _reproducir_audio_local(archivo_clonada)
        emitir_evento_sync("status", {"state": "STANDBY"})
        return

    # 2. Si hay API Key + Voice ID con saldo en .env, transmite la voz clonada en vivo
    if _sintetizar_voz_clonada(texto, archivo_clonada, reproducir_en_vivo=True):
        emitir_evento_sync("status", {"state": "STANDBY"})
        return

    # 3. Si la caché HUD ya existe o se genera con el filtro acústico del casco
    archivo_hud = _ruta_cache_tts(texto, es_clonada=False)
    if os.path.exists(archivo_hud) and os.path.getsize(archivo_hud) > 256:
        _reproducir_audio_local(archivo_hud)
        emitir_evento_sync("status", {"state": "STANDBY"})
        return

    if not _generar_wav_hud(texto, archivo_hud, reproducir_en_vivo=True):
        try:
            import edge_tts

            mp3_fallback = archivo_hud.replace(".wav", ".mp3")
            asyncio.run(edge_tts.Communicate(texto, JARVIS_VOICE, rate=JARVIS_RATE, pitch=JARVIS_PITCH).save(mp3_fallback))
            _reproducir_audio_local(mp3_fallback)
        except Exception as e:
            print(f"[Error TTS]: {e}")

    emitir_evento_sync("status", {"state": "STANDBY"})


def _podar_cache_tts(max_archivos: int = 80):
    """Mantiene acotado tts_cache/ eliminando los audios más antiguos si supera el límite."""
    try:
        archivos = [
            os.path.join(TTS_CACHE_DIR, f)
            for f in os.listdir(TTS_CACHE_DIR)
            if f.endswith((".wav", ".mp3"))
        ]
        if len(archivos) <= max_archivos:
            return
        archivos.sort(key=os.path.getmtime, reverse=True)
        for viejo in archivos[max_archivos:]:
            try:
                os.remove(viejo)
            except OSError:
                pass
    except Exception:
        pass


def _emitir_saludo_inicial():
    """Reproduce un saludo breve al iniciar Windows usando Piper local."""
    try:
        from jarvis_startup import generar_texto_briefing, obtener_clima

        clima = obtener_clima()
        try:
            mensaje = generar_texto_briefing(clima)
        except Exception:
            ahora = datetime.datetime.now().strftime("%H:%M")
            mensaje = (
                f"Buenas de nuevo, jefe. Son las {ahora} y afuera hacen {clima}. "
                "Todos los sistemas principales están en línea."
            )
        hablar_sync(mensaje)
    except Exception as e:
        print(f"[Aviso saludo inicial]: {e}")


def _precalentar_sistema():
    """Pre-carga Piper local en memoria, caché de apps instaladas y poda caché vieja."""
    threading.Thread(target=precalentar_apps_instaladas, daemon=True).start()
    _podar_cache_tts(max_archivos=80)
    frases_comunes = FRASES_PRECALENTAMIENTO
    usar_nube = (os.getenv("JARVIS_TTS_ENGINE") or "piper").strip().lower() == "cloud"
    if not usar_nube and piper_disponible():
        for frase in frases_comunes:
            ruta_piper = _ruta_cache_piper(frase)
            if not os.path.exists(ruta_piper):
                sintetizar_piper(frase, ruta_piper)
        # Asegura el proceso Piper caliente aunque todas las frases ya estuvieran en caché
        ruta_warmup = _ruta_cache_piper(frases_comunes[0])
        if os.path.exists(ruta_warmup):
            from tts_piper import _asegurar_proceso, DEFAULT_PIPER_MODEL
            _asegurar_proceso(os.getenv("PIPER_MODEL") or DEFAULT_PIPER_MODEL)
        print(">>> Voz local Piper TTS lista (sin latencia de red).")
        if os.getenv("JARVIS_SALUDO_INICIO") == "1":
            _emitir_saludo_inicial()
        return

    for frase in frases_comunes:
        ruta_clon = _ruta_cache_tts(frase, es_clonada=True)
        if not os.path.exists(ruta_clon):
            if not _sintetizar_voz_clonada(frase, ruta_clon, reproducir_en_vivo=False):
                ruta_hud = _ruta_cache_tts(frase, es_clonada=False)
                if not os.path.exists(ruta_hud):
                    _generar_wav_hud(frase, ruta_hud, reproducir_en_vivo=False)
    print(f">>> Voz táctica J.A.R.V.I.S. ({JARVIS_VOICE} + filtro acústico Mark III HUD) lista.")
    if os.getenv("JARVIS_SALUDO_INICIO") == "1":
        _emitir_saludo_inicial()


def _stream_piper_oraciones() -> tuple[queue.Queue, threading.Thread]:
    """Sintetiza en paralelo cada oración apenas llega y la reproduce sin esperar la siguiente."""
    cola_texto: queue.Queue[str | None] = queue.Queue()
    cola_wav: queue.Queue[tuple[str, bool] | None] = queue.Queue()

    def _sintetizador():
        try:
            while True:
                oracion = cola_texto.get()
                if oracion is None:
                    break
                ruta_cache = _ruta_cache_piper(oracion)
                if os.path.exists(ruta_cache) and os.path.getsize(ruta_cache) > 256:
                    cola_wav.put((ruta_cache, False))
                    continue
                tmp_wav = os.path.join(TTS_CACHE_DIR, f"_stream_{threading.get_ident()}_{time.perf_counter_ns()}.wav")
                if sintetizar_piper(oracion, tmp_wav):
                    cola_wav.put((tmp_wav, True))
        finally:
            cola_wav.put(None)

    def _reproductor():
        emitido_speaking = False
        while True:
            item = cola_wav.get()
            if item is None:
                break
            wav_path, borrar = item
            try:
                if not emitido_speaking:
                    emitir_evento_sync("status", {"state": "SPEAKING"})
                    emitido_speaking = True
                _reproducir_audio_local(wav_path)
            finally:
                if borrar and os.path.exists(wav_path):
                    try:
                        os.remove(wav_path)
                    except OSError:
                        pass

    h_synth = threading.Thread(target=_sintetizador, daemon=True)
    h_play = threading.Thread(target=_reproductor, daemon=True)
    h_synth.start()
    h_play.start()

    class _StreamJoiner:
        def join(self_inner):
            cola_texto.put(None)
            h_synth.join(timeout=30)
            h_play.join(timeout=60)

    return cola_texto, _StreamJoiner()


def ejecutar_comando_completo(comando: str, audio_wav_bytes: bytes | None = None):
    """Procesa un comando de voz o texto, priorizando fast-path local antes de crear hilos de streaming."""
    ya_ocupado = _ocupado_comando.is_set()
    _ocupado_comando.set()
    try:
        emitir_evento_sync("log", {"sender": "USER", "text": comando})
        emitir_evento_sync("status", {"state": "PROCESSING"})

        # 1. Atajo local instantáneo (<30ms) sin levantar hilos de streaming Piper ni llamar al LLM
        t0 = time.perf_counter()
        respuesta_rapida = _intentar_ejecucion_instantanea(comando)
        if respuesta_rapida is not None:
            print(f"[Latencia Fast-Path Local]: {(time.perf_counter()-t0)*1000:.0f} ms")
            _registrar_turno_historial(comando, respuesta_rapida)
            emitir_evento_sync("log", {"sender": "JARVIS", "text": respuesta_rapida})
            hablar_sync(respuesta_rapida)
            return

        _cargar_env_si_cambio()
        usar_stream_piper = (
            (os.getenv("JARVIS_TTS_ENGINE") or "piper").strip().lower() != "cloud"
            and piper_disponible()
            and not rvc_disponible()
        )

        if usar_stream_piper:
            cola_oraciones, joiner = _stream_piper_oraciones()
            try:
                texto_final, ya_hablado = consultar_gemini_con_tools(
                    comando,
                    audio_wav_bytes=audio_wav_bytes,
                    on_sentence=cola_oraciones.put,
                )
            finally:
                joiner.join()
            emitir_evento_sync("log", {"sender": "JARVIS", "text": texto_final})
            if not ya_hablado and texto_final:
                hablar_sync(texto_final)
            else:
                emitir_evento_sync("status", {"state": "STANDBY"})
        else:
            texto_final, _ = consultar_gemini_con_tools(comando, audio_wav_bytes=audio_wav_bytes)
            emitir_evento_sync("log", {"sender": "JARVIS", "text": texto_final})
            hablar_sync(texto_final)
    except Exception as e:
        print(f"[Error procesando comando]: {e}")
        mensaje_err = f"Error al procesar la directiva: {e}"
        emitir_evento_sync("log", {"sender": "JARVIS", "text": mensaje_err})
        emitir_evento_sync("status", {"state": "STANDBY"})
    finally:
        if not ya_ocupado:
            time.sleep(0.12)
            _ocupado_comando.clear()


def _prefijo_valido_wake(prefijo: str, es_fuerte: bool) -> bool:
    """Evita falsos disparos en medio de charlas largas permitiendo saludos cortos ('che jarvis', 'hey jarvis')."""
    palabras = re.findall(r"[a-záéíóúüñ]+", (prefijo or "").lower())
    if not palabras:
        return True
    if es_fuerte:
        return len(palabras) <= 2 or (len(palabras) <= 3 and all(p in _PREFIJOS_SALUDO_WAKE for p in palabras))
    return len(palabras) <= 2 and all(p in _PREFIJOS_SALUDO_WAKE for p in palabras)


def _extraer_comando_tras_wake_word(texto: str) -> tuple[bool, str]:
    texto_limpio = (texto or "").strip()
    if not texto_limpio:
        return False, ""

    for regex, es_fuerte in ((WAKE_WORD_FUERTE_REGEX, True), (WAKE_WORD_DEBIL_REGEX, False)):
        for m in regex.finditer(texto_limpio):
            prefijo = texto_limpio[: m.start()].strip()
            if _prefijo_valido_wake(prefijo, es_fuerte):
                resto = texto_limpio[m.end() :].strip(" ,.:;¡!¿?-")
                return True, resto
    return False, ""


_GOOGLE_STT_URL = "https://www.google.com/speech-api/v2/recognize"
_GOOGLE_STT_PARAMS = {
    "client": "chromium",
    "lang": "es-AR",
    "key": "AIzaSyBOti4mM-6x9WDnZIjIeyEU21OpBXqWBgw",
    "pFilter": 0,
}
_stt_session = requests.Session()
_stt_session_lock = threading.Lock()


def _parsear_alternativas_google(body: str) -> list[str]:
    for linea in (body or "").splitlines():
        linea = linea.strip()
        if not linea:
            continue
        try:
            datos = json.loads(linea)
        except json.JSONDecodeError:
            continue
        resultados = datos.get("result") or []
        if resultados:
            alts = resultados[0].get("alternative") or []
            textos = [alt.get("transcript", "").strip() for alt in alts if alt.get("transcript")]
            if textos:
                return textos
    return []


def _reconocer_con_alternativas(reconocedor: sr.Recognizer, audio: sr.AudioData) -> str:
    """Transmite PCM16 crudo (audio/l16) por HTTPS Keep-Alive sin lanzar flac-win32.exe ni abrir TLS cada vez."""
    global _stt_session
    pcm_data = audio.get_raw_data(convert_width=2)
    headers = {"Content-Type": f"audio/l16; rate={audio.sample_rate}"}
    timeout = reconocedor.operation_timeout or 6

    ultimo_error: Exception | None = None
    for intento in range(2):
        try:
            with _stt_session_lock:
                sess = _stt_session
            resp = sess.post(
                _GOOGLE_STT_URL,
                params=_GOOGLE_STT_PARAMS,
                headers=headers,
                data=pcm_data,
                timeout=timeout,
            )
            if resp.status_code != 200:
                raise sr.RequestError(f"HTTP {resp.status_code}")
            alternativas = _parsear_alternativas_google(resp.text)
            if not alternativas:
                raise sr.UnknownValueError()
            for alt in alternativas:
                detectado, _ = _extraer_comando_tras_wake_word(alt)
                if detectado:
                    return alt
            return alternativas[0]
        except sr.UnknownValueError:
            raise
        except requests.RequestException as e:
            ultimo_error = e
            if intento == 0:
                with _stt_session_lock:
                    if _stt_session is sess:
                        try:
                            _stt_session.close()
                        except Exception:
                            pass
                        _stt_session = requests.Session()
                continue
            raise sr.RequestError(str(e))
    raise sr.RequestError(str(ultimo_error or "Error STT"))


def _audio_tiene_voz_valida(audio: sr.AudioData) -> bool:
    """Descarta micro-ruidos, clics de teclado o silencios (<0.32s o RMS muy bajo) antes de llamar a Google STT."""
    raw = audio.frame_data
    if not raw or audio.sample_rate <= 0 or audio.sample_width <= 0:
        return False
    duracion = len(raw) / float(audio.sample_rate * audio.sample_width)
    if duracion < 0.32:
        return False
    try:
        rms = audioop.rms(raw, audio.sample_width)
    except Exception:
        return True
    return rms >= 45


# --- BUCLE DE ESCUCHA OPTIMIZADO (PRODUCTOR-CONSUMIDOR NO BLOQUEANTE, 16 kHz NATIVO) ---
_INTERRUMPIR_LECTURA = object()


class _InterrupcionEscucha(Exception):
    """Interrumpe listen(timeout=None) en <1ms sin perder frames previos cuando Jarvis va a hablar."""


class _StreamMicrofonoCola:
    """Stream de lectura con timeout alimentado por callback de PyAudio para evitar bloqueos en C."""

    def __init__(self, pyaudio_stream, cola: queue.Queue, sample_width: int):
        self._stream = pyaudio_stream
        self._cola = cola
        self._sample_width = sample_width
        self._buffer = bytearray()
        self._cerrado = False

    def read(self, size: int) -> bytes:
        bytes_necesarios = size * self._sample_width
        while len(self._buffer) < bytes_necesarios:
            if self._cerrado:
                raise OSError("[Errno -9988] Stream closed")
            try:
                trozo = self._cola.get(timeout=2.5)
            except queue.Empty:
                # Si PortAudio deja de entregar audio (sleep USB, desconexión o cuelgue MME), forzar recuperación.
                raise OSError("Micrófono sin señal (timeout leyendo stream)")
            if trozo is _INTERRUMPIR_LECTURA:
                raise _InterrupcionEscucha()
            if trozo is None:
                raise OSError("[Errno -9988] Stream closed")
            self._buffer.extend(trozo)
        salida = bytes(self._buffer[:bytes_necesarios])
        del self._buffer[:bytes_necesarios]
        return salida

    def interrumpir(self):
        """Despierta inmediatamente una lectura en curso para pausar/cambiar de modo sin perder pre-roll."""
        if self._cerrado:
            return
        if self._cola.full():
            try:
                self._cola.get_nowait()
            except queue.Empty:
                pass
        try:
            self._cola.put_nowait(_INTERRUMPIR_LECTURA)
        except queue.Full:
            pass

    def drain(self):
        """Descarta audio viejo o eco residual sin cerrar ni reabrir el stream de Windows."""
        self._buffer.clear()
        while True:
            try:
                self._cola.get_nowait()
            except queue.Empty:
                break

    def close(self):
        self._cerrado = True
        try:
            self._cola.put_nowait(None)
        except queue.Full:
            pass
        try:
            if self._stream.is_active():
                self._stream.stop_stream()
        except Exception:
            pass
        try:
            self._stream.close()
        except Exception:
            pass


_VERBOS_INCOMPLETOS = {
    "abrí": ("abrí", "¿Qué querés abrir?"),
    "abri": ("abrí", "¿Qué querés abrir?"),
    "abrir": ("abrí", "¿Qué querés abrir?"),
    "abrime": ("abrí", "¿Qué querés abrir?"),
    "me abrís": ("abrí", "¿Qué querés abrir?"),
    "me abris": ("abrí", "¿Qué querés abrir?"),
    "cerrá": ("cerrá", "¿Qué querés cerrar?"),
    "cerra": ("cerrá", "¿Qué querés cerrar?"),
    "cerrar": ("cerrá", "¿Qué querés cerrar?"),
    "cerrame": ("cerrá", "¿Qué querés cerrar?"),
    "poné": ("poné", "¿Qué tema pongo?"),
    "pone": ("poné", "¿Qué tema pongo?"),
    "poneme": ("poné", "¿Qué tema pongo?"),
    "reproducí": ("reproducí", "¿Qué tema pongo?"),
    "reproduci": ("reproducí", "¿Qué tema pongo?"),
    "reproducir": ("reproducí", "¿Qué tema pongo?"),
    "buscá": ("buscá", "¿Qué querés buscar?"),
    "busca": ("buscá", "¿Qué querés buscar?"),
    "buscar": ("buscá", "¿Qué querés buscar?"),
    "buscame": ("buscá", "¿Qué querés buscar?"),
}


class _MicrofonoResiliente(sr.AudioSource):
    """Fuente de audio no bloqueante a 16 kHz nativo (2.75x menos CPU/datos que 44.1 kHz) con fallback automático."""

    def __init__(self, device_index: int | None = None, preferred_rate: int = 16000):
        import pyaudio

        self.device_index = device_index
        self.preferred_rate = preferred_rate
        self.format = pyaudio.paInt16
        self.SAMPLE_WIDTH = pyaudio.get_sample_size(self.format)
        self.SAMPLE_RATE = preferred_rate
        self.CHUNK = 480  # 30ms exactos a 16 kHz
        self.audio = None
        self.stream: _StreamMicrofonoCola | None = None
        self._actualizar_info_dispositivo()

    def _actualizar_info_dispositivo(self, pa=None):
        import pyaudio

        propio = pa is None
        instancia = pa or pyaudio.PyAudio()
        try:
            info = (
                instancia.get_device_info_by_index(self.device_index)
                if self.device_index is not None
                else instancia.get_default_input_device_info()
            )
            dev_idx = int(info["index"])
            rate_elegida = None
            try:
                if instancia.is_format_supported(
                    self.preferred_rate,
                    input_device=dev_idx,
                    input_channels=1,
                    input_format=self.format,
                ):
                    rate_elegida = self.preferred_rate
            except Exception:
                pass
            if rate_elegida is None:
                rate_elegida = int(info.get("defaultSampleRate") or 44100)
            self.SAMPLE_RATE = rate_elegida if rate_elegida > 0 else 44100
            self.CHUNK = 480 if self.SAMPLE_RATE == 16000 else 1024
        finally:
            if propio:
                instancia.terminate()

    def __enter__(self):
        import pyaudio

        assert self.stream is None, "El micrófono ya está abierto"
        self.audio = pyaudio.PyAudio()
        cola: queue.Queue[bytes | None] = queue.Queue(maxsize=64)

        def _callback(in_data, frame_count, time_info, status_flags):
            if cola.full():
                try:
                    cola.get_nowait()
                except queue.Empty:
                    pass
            try:
                cola.put_nowait(in_data)
            except queue.Full:
                pass
            return (None, pyaudio.paContinue)

        try:
            self._actualizar_info_dispositivo(self.audio)
            pyaudio_stream = self.audio.open(
                input_device_index=self.device_index,
                channels=1,
                format=self.format,
                rate=self.SAMPLE_RATE,
                frames_per_buffer=self.CHUNK,
                input=True,
                stream_callback=_callback,
            )
            pyaudio_stream.start_stream()
            self.stream = _StreamMicrofonoCola(pyaudio_stream, cola, self.SAMPLE_WIDTH)
            return self
        except Exception:
            try:
                self.audio.terminate()
            except Exception:
                pass
            self.audio = None
            self.stream = None
            raise

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if self.stream is not None:
                self.stream.close()
        finally:
            self.stream = None
            if self.audio is not None:
                try:
                    self.audio.terminate()
                except Exception:
                    pass
                self.audio = None


def worker_reconocimiento():
    reconocedor = sr.Recognizer()
    # Parámetros equilibrados: no corta frases a mitad ni recorta el inicio de "Jarvis"
    reconocedor.pause_threshold = 0.65
    reconocedor.non_speaking_duration = 0.32
    reconocedor.phrase_threshold = 0.22
    reconocedor.dynamic_energy_threshold = True
    reconocedor.dynamic_energy_adjustment_damping = 0.15
    reconocedor.dynamic_energy_ratio = 1.30
    reconocedor.operation_timeout = 6

    def _acotar_umbral():
        reconocedor.energy_threshold = max(65.0, min(420.0, float(reconocedor.energy_threshold)))

    def _abrir_y_calibrar() -> _MicrofonoResiliente:
        mic = _MicrofonoResiliente()
        reconocedor.energy_threshold = 180.0
        with mic as fuente:
            reconocedor.adjust_for_ambient_noise(fuente, duration=0.6)
        _acotar_umbral()
        return mic

    microfono: _MicrofonoResiliente | None = None
    ultimo_aviso_mic = ""
    while microfono is None:
        try:
            microfono = _abrir_y_calibrar()
        except Exception as e:
            msg = str(e)
            if msg != ultimo_aviso_mic:
                print(f"[Aviso Micrófono]: Esperando micrófono disponible ({e}).")
                ultimo_aviso_mic = msg
            time.sleep(3)

    print(
        f">>> Micrófono calibrado ({microfono.SAMPLE_RATE} Hz, L16 Keep-Alive, "
        f"umbral={reconocedor.energy_threshold:.0f}). Escucha continua activa..."
    )

    # 2 workers STT concurrentes para que frases previas de fondo nunca retrasen una orden "Jarvis..."
    cola_stt: queue.Queue[sr.AudioData] = queue.Queue(maxsize=3)
    cola_orden_directa: queue.Queue[sr.AudioData | None] = queue.Queue(maxsize=1)
    pedir_orden_directa = threading.Event()
    comando_lock = threading.Lock()
    stream_activo: list[_StreamMicrofonoCola | None] = [None]

    def _interrumpir_escucha_pasiva():
        s = stream_activo[0]
        if s is not None:
            s.interrumpir()

    def _drenar_cola(q: queue.Queue):
        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                break

    def _esperar_audio_directo() -> sr.AudioData | None:
        _drenar_cola(cola_orden_directa)
        pedir_orden_directa.set()
        _interrumpir_escucha_pasiva()
        try:
            return cola_orden_directa.get(timeout=7.5)
        except queue.Empty:
            return None
        finally:
            pedir_orden_directa.clear()

    def _consumidor_stt():
        while True:
            audio = cola_stt.get()
            if _ocupado_comando.is_set():
                continue
            try:
                texto = _reconocer_con_alternativas(reconocedor, audio)
            except sr.UnknownValueError:
                continue
            except sr.RequestError as e:
                print(f"[Aviso STT]: {e}")
                time.sleep(0.5)
                continue
            except Exception as e:
                print(f"[Error STT]: {e}")
                continue

            print(f"[Micrófono capturó]: {texto}")
            detectado, comando = _extraer_comando_tras_wake_word(texto)
            if not detectado:
                continue

            if not comando_lock.acquire(blocking=False):
                continue

            try:
                if _ocupado_comando.is_set():
                    continue
                print(f"[!] Wake word activada -> comando inicial: '{comando or '(esperando orden)'}'")
                _ocupado_comando.set()
                _interrumpir_escucha_pasiva()
                _drenar_cola(cola_stt)
                emitir_evento_sync("status", {"state": "LISTENING"})

                audio_orden: sr.AudioData | None = audio
                comando_pendiente = comando
                verbo_incompleto = _VERBOS_INCOMPLETOS.get(comando_pendiente.lower().strip(" ,.:;¡!¿?-"))

                if not comando_pendiente or verbo_incompleto is not None:
                    pregunta = verbo_incompleto[1] if verbo_incompleto else elegir_saludo_wake()
                    hablar_sync(pregunta)
                    emitir_evento_sync("status", {"state": "LISTENING"})
                    audio_orden = _esperar_audio_directo()
                    if audio_orden is None:
                        emitir_evento_sync("status", {"state": "STANDBY"})
                        continue
                    try:
                        respuesta_usr = _reconocer_con_alternativas(reconocedor, audio_orden).strip()
                    except (sr.UnknownValueError, sr.RequestError):
                        respuesta_usr = "(orden de voz)"
                    _, limpio = _extraer_comando_tras_wake_word(respuesta_usr)
                    if limpio:
                        respuesta_usr = limpio
                    if verbo_incompleto and respuesta_usr != "(orden de voz)":
                        comando_pendiente = f"{verbo_incompleto[0]} {respuesta_usr}"
                    else:
                        comando_pendiente = respuesta_usr

                if comando_pendiente:
                    print(f"[Orden detectada]: {comando_pendiente}")
                    wav_bytes = (
                        audio_orden.get_wav_data(convert_rate=16000, convert_width=2)
                        if audio_orden is not None and _requiere_audio_multimodal(comando_pendiente)
                        else None
                    )
                    ejecutar_comando_completo(comando_pendiente, audio_wav_bytes=wav_bytes)
                else:
                    emitir_evento_sync("status", {"state": "STANDBY"})
            except Exception as e:
                print(f"[Error ejecutando orden]: {e}")
                emitir_evento_sync("status", {"state": "STANDBY"})
            finally:
                _drenar_cola(cola_stt)
                time.sleep(0.12)
                _ocupado_comando.clear()
                comando_lock.release()

    for _ in range(2):
        threading.Thread(target=_consumidor_stt, daemon=True).start()

    necesita_recalibrar = False
    ultimo_error_worker = ""
    ultimo_log_error = 0.0

    while True:
        try:
            if necesita_recalibrar or microfono is None:
                microfono = _abrir_y_calibrar()
                necesita_recalibrar = False
                ultimo_error_worker = ""
                print(f">>> Micrófono recuperado ({microfono.SAMPLE_RATE} Hz). Escucha continua activa...")

            # Stream siempre abierto: timeout=None preserva el pre-roll completo sin cortar el inicio de "Jarvis"
            with microfono as fuente:
                stream_activo[0] = fuente.stream
                estaba_ocupado = False
                try:
                    while True:
                        if pedir_orden_directa.is_set():
                            fuente.stream.drain()
                            try:
                                audio_dir = reconocedor.listen(fuente, timeout=6.0, phrase_time_limit=12)
                            except (sr.WaitTimeoutError, _InterrupcionEscucha):
                                audio_dir = None
                            _acotar_umbral()
                            pedir_orden_directa.clear()
                            try:
                                cola_orden_directa.put_nowait(audio_dir)
                            except queue.Full:
                                pass
                            estaba_ocupado = True
                            continue

                        if _ocupado_comando.is_set():
                            fuente.stream.drain()
                            estaba_ocupado = True
                            time.sleep(0.04)
                            continue

                        if estaba_ocupado:
                            fuente.stream.drain()
                            estaba_ocupado = False

                        try:
                            audio = reconocedor.listen(fuente, timeout=None, phrase_time_limit=12)
                        except _InterrupcionEscucha:
                            continue

                        _acotar_umbral()
                        if _ocupado_comando.is_set() or not _audio_tiene_voz_valida(audio):
                            continue

                        if cola_stt.full():
                            try:
                                cola_stt.get_nowait()
                            except queue.Empty:
                                pass
                        try:
                            cola_stt.put_nowait(audio)
                        except queue.Full:
                            pass
                finally:
                    stream_activo[0] = None

        except Exception as e:
            msg = str(e)
            ahora = time.monotonic()
            if msg != ultimo_error_worker or (ahora - ultimo_log_error) > 15.0:
                print(f"[Error worker]: {msg}")
                ultimo_error_worker = msg
                ultimo_log_error = ahora
            if pedir_orden_directa.is_set():
                pedir_orden_directa.clear()
                try:
                    cola_orden_directa.put_nowait(None)
                except queue.Full:
                    pass
            emitir_evento_sync("status", {"state": "STANDBY"})
            necesita_recalibrar = True
            time.sleep(1.0)


# --- TELEMETRÍA PERIÓDICA (CPU, RAM, SPOTIFY) ---
async def bucle_telemetria():
    tick = 0
    ultima_cancion = "Spotify listo"
    while True:
        if not manager.active_connections:
            await asyncio.sleep(5.0)
            continue

        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory().percent
        # Consultar Spotify API cada 4 ciclos (~10s) para ahorrar red/CPU; el stream local ya es instantáneo
        if tick % 4 == 0:
            ultima_cancion = await asyncio.to_thread(obtener_cancion_actual, False)
        tick += 1

        await manager.broadcast(
            {
                "type": "telemetry",
                "cpu": cpu,
                "ram": ram,
                "spotify": ultima_cancion,
            }
        )
        await asyncio.sleep(2.5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global loop_principal
    loop_principal = asyncio.get_running_loop()

    tarea_telemetria = asyncio.create_task(bucle_telemetria())

    threading.Thread(target=_precalentar_sistema, daemon=True).start()

    hilo = threading.Thread(target=worker_reconocimiento, daemon=True)
    hilo.start()

    yield
    tarea_telemetria.cancel()
    cerrar_piper()
    cerrar_rvc()


# --- APLICACIÓN FASTAPI ---
app = FastAPI(title="J.A.R.V.I.S. Neural Backend", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def serve_hud():
    return FileResponse(os.path.join(BASE_DIR, "jarvis_hud_interface.html"))


@app.post("/api/fish-session")
async def create_fish_agent_session():
    """Genera un session token de Fish Audio para el agente 7e8e33caac214c3996800490800910e0."""
    _cargar_env_si_cambio()
    api_key = (os.getenv("FISH_API_KEY") or os.getenv("JARVIS_VOICE_API_KEY") or "").strip()
    agent_id = (os.getenv("FISH_AGENT_ID") or "7e8e33caac214c3996800490800910e0").strip()
    if not api_key:
        return {"error": "Falta configurar FISH_API_KEY en el archivo .env"}

    req = urlrequest.Request(
        "https://api.fish.audio/v1/agent/sessions",
        data=json.dumps({"agent_id": agent_id}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "JARVIS-HUD/2.0",
        },
        method="POST",
    )
    with urlrequest.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            raw_msg = await websocket.receive_text()
            try:
                data = json.loads(raw_msg)
                if isinstance(data, dict) and data.get("type") == "command" and data.get("text"):
                    threading.Thread(
                        target=ejecutar_comando_completo,
                        args=(data["text"],),
                        daemon=True,
                    ).start()
            except Exception:
                pass
    except WebSocketDisconnect:
        manager.disconnect(websocket)


_instance_mutex = None


def _adquirir_instancia_unica() -> bool:
    """Evita duplicar Jarvis en segundo plano si ya hay una instancia escuchando."""
    global _instance_mutex
    if os.name != "nt":
        return True
    _instance_mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\JARVIS_Neural_Backend_Mutex")
    return ctypes.windll.kernel32.GetLastError() != 183


if __name__ == "__main__":
    if not _adquirir_instancia_unica():
        sys.exit(0)
    if "--saludo" in sys.argv:
        os.environ["JARVIS_SALUDO_INICIO"] = "1"
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
        log_config=None if _ES_HEADLESS else uvicorn.config.LOGGING_CONFIG,
    )
