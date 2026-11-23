import os
import ctypes
import asyncio
import datetime
import webbrowser
import requests
import edge_tts
from google import genai

try:
    import pygame
    HAS_PYGAME = True
except ImportError:
    HAS_PYGAME = False

# Directorio base del script (importante cuando se ejecuta al inicio de Windows)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 1. Configuración de API y Ubicación
from dotenv import load_dotenv

load_dotenv(os.path.join(BASE_DIR, ".env"))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Coordenadas (por defecto Buenos Aires; podés ajustar lat/lon)
LAT, LON = -34.6037, -58.3816
VOZ = "es-AR-TomasNeural"  # Alternativa más grave: "es-ES-AlvaroNeural"


def obtener_clima():
    """Consulta temperatura y estado del cielo sin API key vía Open-Meteo."""
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={LAT}&longitude={LON}&current=temperature_2m,weather_code&timezone=auto"
        )
        res = requests.get(url, timeout=5).json()["current"]
        temp = res["temperature_2m"]
        return f"{temp}°C"
    except Exception:
        return "No disponible"


def generar_texto_briefing(clima: str):
    ahora = datetime.datetime.now()
    hora_str = ahora.strftime("%H:%M")
    fecha_str = ahora.strftime("%A %d de %B")

    prompt = f"""
    Eres J.A.R.V.I.S., un asistente de IA brillante, ingenioso, con humor agudo y muy buena onda (en español rioplatense/argentino).
    El usuario acaba de encender su terminal de trabajo.
    Datos actuales:
    - Hora: {hora_str}
    - Fecha: {fecha_str}
    - Clima exterior: {clima}

    Genera un saludo de bienvenida breve (máximo 3 frases concisas) con un toque de humor o chispa para arrancar el día.
    Informa la hora, el clima y confirma que todos los sistemas principales están operativos.
    No uses emojis ni formato markdown complejo, solo texto listo para ser leído en voz alta.
    """

    client = genai.Client(api_key=GEMINI_API_KEY)
    interaction = client.interactions.create(
        model="gemini-3.5-flash-lite",
        input=prompt,
        generation_config={"thinking_level": "minimal"},
    )
    return interaction.output_text.strip()


async def sintetizar_voz(texto: str, output_file: str):
    comunicador = edge_tts.Communicate(texto, VOZ)
    await comunicador.save(output_file)


def reproducir_audio(archivo_audio: str):
    archivo_abs = os.path.abspath(archivo_audio)
    if archivo_abs.endswith(".wav"):
        try:
            import winsound
            winsound.PlaySound(archivo_abs, winsound.SND_FILENAME)
            if os.path.exists(archivo_abs):
                os.remove(archivo_abs)
            return
        except Exception:
            pass
    if HAS_PYGAME:
        pygame.mixer.init()
        pygame.mixer.music.load(archivo_abs)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            pygame.time.Clock().tick(10)
        pygame.mixer.quit()
    else:
        # Reproducción nativa de Windows vía winmm.dll (MCI) sin abrir ventanas
        mci = ctypes.windll.winmm.mciSendStringW
        mci(f'open "{archivo_abs}" type mpegvideo alias jarvis_tts', None, 0, None)
        mci("play jarvis_tts wait", None, 0, None)
        mci("close jarvis_tts", None, 0, None)

    if os.path.exists(archivo_abs):
        os.remove(archivo_abs)


def main():
    local_dir = os.path.join(os.getenv("LOCALAPPDATA") or BASE_DIR, "JARVIS")
    os.makedirs(local_dir, exist_ok=True)
    archivo_mp3 = os.path.join(local_dir, "saludo.mp3")
    archivo_wav = os.path.join(local_dir, "saludo.wav")
    hud_html = os.path.join(BASE_DIR, "jarvis_hud_interface.html")

    # Obtener telemetría
    clima = obtener_clima()

    # Redacción con Gemini (con respaldo en caso de no tener API Key o conexión aún)
    try:
        mensaje = generar_texto_briefing(clima)
    except Exception:
        ahora = datetime.datetime.now().strftime("%H:%M")
        mensaje = (
            f"Buenas de nuevo, jefe. Son las {ahora} y afuera hacen {clima}. "
            "Todos los sistemas principales están en línea y listos para lo que venga."
        )

    # Síntesis local con Piper (sin latencia de red); respaldo en edge-tts
    try:
        from tts_piper import piper_disponible, sintetizar_piper, cerrar_piper

        if piper_disponible() and sintetizar_piper(mensaje, archivo_wav):
            cerrar_piper()
            reproducir_audio(archivo_wav)
            return
    except Exception:
        pass

    asyncio.run(sintetizar_voz(mensaje, archivo_mp3))

    # Opcional: Levantar la interfaz web simultáneamente si deseas descomentar la línea siguiente:
    # if os.path.exists(hud_html):
    #     webbrowser.open(f"file:///{hud_html.replace(os.sep, '/')}")

    # Hablar
    reproducir_audio(archivo_mp3)


if __name__ == "__main__":
    main()
