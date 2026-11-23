import os
import re
import json
import base64
import ctypes
import asyncio
from collections import deque
from contextlib import nullcontext
import edge_tts
import speech_recognition as sr
from google import genai
from google.genai import types
from tools_windows import DECLARACIONES_HERRAMIENTAS, MAPA_HERRAMIENTAS
from jarvis_persona import SYSTEM_INSTRUCTION, elegir_saludo_wake, respuesta_oral_tool

try:
    import pygame
    HAS_PYGAME = True
except ImportError:
    HAS_PYGAME = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Configuración de Gemini
from dotenv import load_dotenv

load_dotenv(os.path.join(BASE_DIR, ".env"))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise ValueError("No se encontró GEMINI_API_KEY en el archivo .env")
client = genai.Client(api_key=GEMINI_API_KEY)

VOZ = "es-AR-TomasNeural"
WAKE_WORD = "jarvis"
WAKE_WORDS = (
    "jarvis", "harvis", "sharvis", "charvis", "yarvis", "garvis", "darvis", "arvis",
    "jarvi", "harvi", "sharvi", "charvi", "yarvi", "arvi",
    "jarbi", "harbi", "sharbi", "charbi", "yarbi", "arbi",
    "jarbis", "harbis", "sharbis", "charbis", "yarbis", "arbis",
    "harvey", "jarvey", "harvy", "jarvy", "yarvey",
    "service", "servis", "sarvis", "sarvia",
    "yavis", "javis", "havis", "shavis", "chavis",
    "xavi", "shabi", "chabi", "yabi", "jabi", "llavis", "shavi",
    "jarv", "harv", "jarb", "harb", "jar", "har", "shar", "yar",
)
WAKE_WORD_REGEX = re.compile(
    r"\b(?:"
    r"(?:j|h|y|sh|ch|g|d|ll|x)?(?:ar|er)(?:v|b)(?:is|i|ey|y|es|e)?"
    r"|(?:y|j|h|sh|ch|x|ll)a(?:v|b)i(?:s)?"
    r"|s(?:e|a)rvi(?:ce|s|a)"
    r"|jar|har|shar|yar"
    r")\b",
    re.IGNORECASE,
)
MODELO = "gemini-3.5-flash-lite"

_GEMINI_FUNCTION_DECLS = [
    types.FunctionDeclaration(
        name=decl["name"],
        description=decl.get("description", ""),
        parameters_json_schema=decl.get("parameters", {"type": "object", "properties": {}}),
    )
    for decl in DECLARACIONES_HERRAMIENTAS
]

_GEMINI_CONFIG = types.GenerateContentConfig(
    system_instruction=SYSTEM_INSTRUCTION,
    tools=[types.Tool(function_declarations=_GEMINI_FUNCTION_DECLS)],
    thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL),
    temperature=0.45,
)

_CHAT_HISTORIAL: deque[types.Content] = deque(maxlen=10)


def procesar_orden_gemini(
    comando: str,
    previous_id: str | None = None,
    audio_wav_bytes: bytes | None = None,
) -> tuple[str, str | None]:
    """Envía la orden (con audio original si está disponible) a Gemini vía streaming con memoria deslizante."""
    if audio_wav_bytes:
        prompt_audio = (
            f"Escucha el audio original del usuario con prioridad (transcripción aproximada de referencia: '{comando}'). "
            "Ignora la palabra de activación ('Jarvis'/'Shabi') si aparece al inicio. "
            "El STT en español suele deformar nombres en inglés (ej. 'swiss dream' por 'Sweet Dreams', 'confidence' por 'Confident'): "
            "presta especial atención al audio real para entender correctamente canciones o artistas aunque el usuario hable rápido. "
            "Si el audio está cortado, es ambiguo o no contiene un título/artista claro, NO adivines ni pongas cualquier tema: pedí aclaración brevemente."
        )
        user_parts = [
            types.Part.from_bytes(data=audio_wav_bytes, mime_type="audio/wav"),
            types.Part.from_text(text=prompt_audio),
        ]
    else:
        user_parts = [types.Part.from_text(text=comando)]

    contents = list(_CHAT_HISTORIAL) + [types.Content(role="user", parts=user_parts)]
    text_chunks: list[str] = []
    function_calls = []

    stream = client.models.generate_content_stream(
        model=MODELO,
        contents=contents,
        config=_GEMINI_CONFIG,
    )
    for chunk in stream:
        if chunk.function_calls:
            function_calls.extend(chunk.function_calls)
        if chunk.text:
            text_chunks.append(chunk.text)

    respuestas_tools = []
    resultados_log = []
    for fc in function_calls:
        args = dict(fc.args or {})
        fn = MAPA_HERRAMIENTAS.get(fc.name)
        if fn:
            resultado = fn(**args)
        else:
            resultado = f"Herramienta desconocida: {fc.name}"
        print(f"[Tool ejecutada] {fc.name}({args}) -> {resultado}")
        resultados_log.append(f"{fc.name}: {resultado}")
        oral = respuesta_oral_tool(fc.name, resultado)
        if oral and oral not in respuestas_tools:
            respuestas_tools.append(oral)

    _CHAT_HISTORIAL.append(types.Content(role="user", parts=[types.Part.from_text(text=comando)]))

    if respuestas_tools:
        texto_tools = " ".join(respuestas_tools)
        _CHAT_HISTORIAL.append(
            types.Content(
                role="model",
                parts=[types.Part.from_text(text=f"{texto_tools} ({'; '.join(resultados_log)})")],
            )
        )
        return texto_tools, "chat"

    texto_salida = "".join(text_chunks).strip()
    if texto_salida:
        _CHAT_HISTORIAL.append(
            types.Content(role="model", parts=[types.Part.from_text(text=texto_salida)])
        )
    return texto_salida, "chat"


async def hablar_async(texto: str):
    local_dir = os.path.join(os.getenv("LOCALAPPDATA") or BASE_DIR, "JARVIS")
    os.makedirs(local_dir, exist_ok=True)
    archivo = os.path.join(local_dir, "temp_voice.mp3")
    comunicador = edge_tts.Communicate(texto, VOZ)
    await comunicador.save(archivo)

    if HAS_PYGAME:
        pygame.mixer.init()
        pygame.mixer.music.load(archivo)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            pygame.time.Clock().tick(10)
        pygame.mixer.quit()
    else:
        mci = ctypes.windll.winmm.mciSendStringW
        mci(f'open "{archivo}" type mpegvideo alias jarvis_voice', None, 0, None)
        mci("play jarvis_voice wait", None, 0, None)
        mci("close jarvis_voice", None, 0, None)

    if os.path.exists(archivo):
        os.remove(archivo)


def hablar(texto: str):
    try:
        from server import hablar_sync
        hablar_sync(texto)
        return
    except Exception:
        pass
    try:
        import winsound
        from tts_piper import piper_disponible, sintetizar_piper

        if piper_disponible():
            local_dir = os.path.join(os.getenv("LOCALAPPDATA") or BASE_DIR, "JARVIS")
            os.makedirs(local_dir, exist_ok=True)
            wav_tmp = os.path.join(local_dir, "temp_voice.wav")
            if sintetizar_piper(texto, wav_tmp):
                winsound.PlaySound(wav_tmp, winsound.SND_FILENAME)
                if os.path.exists(wav_tmp):
                    os.remove(wav_tmp)
                return
    except Exception:
        pass
    asyncio.run(hablar_async(texto))


def _extraer_comando_tras_wake_word(texto: str) -> tuple[bool, str]:
    texto_limpio = texto.strip()
    m = WAKE_WORD_REGEX.search(texto_limpio)
    if m:
        resto = texto_limpio[m.end() :].strip(" ,.:;¡!¿?-")
        return True, resto

    texto_lower = texto_limpio.lower()
    for w in WAKE_WORDS:
        mw = re.search(rf"\b{re.escape(w)}\b", texto_lower)
        if mw:
            resto = texto_limpio[mw.end() :].strip(" ,.:;¡!¿?-")
            return True, resto
    return False, ""


def escuchar_microfono(reconocedor, microfono, timeout=None, devolver_audio=False):
    ctx = nullcontext(microfono) if getattr(microfono, "stream", None) is not None else microfono
    with ctx as fuente:
        try:
            audio = reconocedor.listen(fuente, timeout=timeout, phrase_time_limit=12)
            respuesta = reconocedor.recognize_google(audio, language="es-AR", show_all=True)
            texto_final = None
            if isinstance(respuesta, dict) and "alternative" in respuesta:
                alternativas = [alt.get("transcript", "").strip() for alt in respuesta["alternative"] if alt.get("transcript")]
                if alternativas:
                    for alt in alternativas:
                        detectado, _ = _extraer_comando_tras_wake_word(alt)
                        if detectado:
                            texto_final = alt.lower()
                            break
                    if not texto_final:
                        texto_final = alternativas[0].lower()
            elif isinstance(respuesta, str) and respuesta.strip():
                texto_final = respuesta.strip().lower()

            if devolver_audio:
                return texto_final, audio
            return texto_final
        except (sr.WaitTimeoutError, sr.UnknownValueError):
            return (None, None) if devolver_audio else None
        except sr.RequestError:
            print("[Error] Falla de red en reconocimiento de voz.")
            return (None, None) if devolver_audio else None


_PATRON_MUSICA_AUDIO = re.compile(
    r"\b(?:pon(?:e|é|eme|éme|er)?|reproduc(?:i|í|ir|e|ime|íme|zca|zcas)?|escuch(?:a|á|ar|ame|áme)?|toc(?:a|á|ar)|pas(?:a|á|ame|áme)|m[uú]sica|musica|canci[oó]n|cancion|tema|artista|[aá]lbum|album|playlist|playlists|lista|listas|radio|radios|mix|me\s+gusta|favorit[ao]s|spotify)\b",
    re.IGNORECASE,
)


def _requiere_audio_multimodal(comando: str) -> bool:
    texto = (comando or "").strip()
    return texto == "(orden de voz)" or bool(_PATRON_MUSICA_AUDIO.search(texto))


def main():
    reconocedor = sr.Recognizer()
    reconocedor.pause_threshold = 0.80
    reconocedor.non_speaking_duration = 0.35
    reconocedor.dynamic_energy_threshold = True
    reconocedor.dynamic_energy_adjustment_damping = 0.15
    reconocedor.dynamic_energy_ratio = 1.45
    microfono = sr.Microphone()
    with microfono as fuente:
        reconocedor.adjust_for_ambient_noise(fuente, duration=0.8)
    previous_interaction_id = None
    turnos_contexto = 0
    print(">>> J.A.R.V.I.S. en modo escucha pasiva (es-AR + audio selectivo Gemini)...")

    while True:
        # 1. Escucha continua buscando la wake word sin reabrir el micrófono en cada silencio
        comando = None
        audio_orden = None
        with microfono as fuente:
            while True:
                texto_capturado, audio_capturado = escuchar_microfono(
                    reconocedor, fuente, timeout=None, devolver_audio=True
                )
                if not texto_capturado:
                    continue

                print(f"[Micrófono capturó]: {texto_capturado}")
                detectado, resto = _extraer_comando_tras_wake_word(texto_capturado)
                if detectado:
                    print(f"\n[!] Wake word detectada: {texto_capturado}")
                    comando = resto
                    audio_orden = audio_capturado
                    break

        # Si solo dijo "Jarvis", da un acuse de recibo fuera del stream para no grabar eco propio
        if not comando:
            hablar(elegir_saludo_wake())
            print(">>> Esperando orden...")
            with microfono as fuente:
                comando, audio_orden = escuchar_microfono(
                    reconocedor, fuente, timeout=6, devolver_audio=True
                )
            if comando:
                _, comando_limpio = _extraer_comando_tras_wake_word(comando)
                if comando_limpio:
                    comando = comando_limpio
            elif audio_orden is not None:
                comando = "(orden de voz)"

        if comando:
            print(f"[Orden del usuario]: {comando}")
            try:
                wav_bytes = (
                    audio_orden.get_wav_data(convert_rate=16000, convert_width=2)
                    if audio_orden is not None and _requiere_audio_multimodal(comando)
                    else None
                )
                if turnos_contexto >= 6:
                    previous_interaction_id = None
                    turnos_contexto = 0
                texto_salida, previous_interaction_id = procesar_orden_gemini(
                    comando, previous_interaction_id, audio_wav_bytes=wav_bytes
                )
                turnos_contexto = turnos_contexto + 1 if previous_interaction_id else 0
                print(f"[J.A.R.V.I.S.]: {texto_salida}")
                if texto_salida:
                    hablar(texto_salida)
            except Exception as e:
                print(f"[Error en procesamiento]: {e}")
                hablar("Ocurrió un error al procesar la instrucción en el sistema.")
        else:
            print("[Cancelado] No se detectó ninguna instrucción.")


if __name__ == "__main__":
    main()
