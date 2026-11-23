# tools_multimedia.py
import ctypes
from ctypes import cast, POINTER
import comtypes
from comtypes import CLSCTX_ALL
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

# --- CONTROL DE VOLUMEN PORCENTUAL (0-100%) ---


def _obtener_endpoint_volumen():
    dispositivo = AudioUtilities.GetSpeakers()
    if hasattr(dispositivo, "EndpointVolume"):
        return dispositivo.EndpointVolume
    interfaz = dispositivo.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(interfaz, POINTER(IAudioEndpointVolume))


def ajustar_volumen(porcentaje: int) -> str:
    """Ajusta el volumen maestro de Windows a un porcentaje específico entre 0 y 100.
    Parámetro 'porcentaje': número entero entre 0 y 100."""
    com_ok = False
    try:
        comtypes.CoInitialize()
        com_ok = True
        nivel = max(0, min(100, int(porcentaje)))
        volumen_endpoint = _obtener_endpoint_volumen()
        volumen_endpoint.SetMute(0, None)
        volumen_endpoint.SetMasterVolumeLevelScalar(nivel / 100.0, None)
        return f"Volumen maestro ajustado al {nivel}%."
    except Exception as e:
        return f"Error al modificar el volumen: {str(e)}"
    finally:
        if com_ok:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass


def ajustar_volumen_delta(delta: int) -> str:
    """Sube o baja el volumen maestro de Windows de forma relativa sin pasar por el LLM."""
    com_ok = False
    try:
        comtypes.CoInitialize()
        com_ok = True
        volumen_endpoint = _obtener_endpoint_volumen()
        actual = int(round(volumen_endpoint.GetMasterVolumeLevelScalar() * 100))
        nivel = max(0, min(100, actual + int(delta)))
        volumen_endpoint.SetMute(0, None)
        volumen_endpoint.SetMasterVolumeLevelScalar(nivel / 100.0, None)
        return f"Volumen maestro ajustado al {nivel}%."
    except Exception as e:
        return f"Error al modificar el volumen: {str(e)}"
    finally:
        if com_ok:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass


# --- CONTROL DE REPRODUCCIÓN MULTIMEDIA (SPOTIFY / WINDOWS) ---

# Códigos de teclas virtuales (VK) de Windows para hardware multimedia
VK_MEDIA_NEXT_TRACK = 0xB0  # Siguiente pista
VK_MEDIA_PREV_TRACK = 0xB1  # Pista anterior
VK_MEDIA_STOP = 0xB2        # Detener
VK_MEDIA_PLAY_PAUSE = 0xB3  # Alternar reproducción/pausa


def controlar_reproduccion(accion: str) -> str:
    """Controla la reproducción global de audio en Windows (Spotify API directa, stream local y teclas multimedia).
    Valores válidos para 'accion':
    - 'play_pause': pausa o reanuda la canción actual.
    - 'pausa': pausa explícitamente la reproducción actual.
    - 'play': reanuda explícitamente la reproducción actual.
    - 'siguiente': pasa al siguiente tema.
    - 'anterior': vuelve al tema anterior o reinicia el actual.
    - 'detener': frena la reproducción."""
    from tools_spotify import alternar_pausa_stream_local, controlar_spotify_api, detener_stream_local

    accion_norm = accion.lower().strip()
    if accion_norm in ("detener", "stop"):
        detener_stream_local()
    elif accion_norm in ("play_pause", "play", "pausa"):
        if alternar_pausa_stream_local():
            return f"Se ejecutó el comando de reproducción: {accion}."

    if controlar_spotify_api(accion_norm):
        return f"Se ejecutó el comando de reproducción: {accion}."

    mapeo = {
        "play_pause": VK_MEDIA_PLAY_PAUSE,
        "play": VK_MEDIA_PLAY_PAUSE,
        "pausa": VK_MEDIA_PLAY_PAUSE,
        "siguiente": VK_MEDIA_NEXT_TRACK,
        "next": VK_MEDIA_NEXT_TRACK,
        "anterior": VK_MEDIA_PREV_TRACK,
        "prev": VK_MEDIA_PREV_TRACK,
        "detener": VK_MEDIA_STOP,
        "stop": VK_MEDIA_STOP,
    }

    vk_code = mapeo.get(accion.lower())
    if not vk_code:
        return f"Comando '{accion}' no reconocido. Opciones válidas: play_pause, siguiente, anterior, detener."

    # Simula presionar y soltar la tecla multimedia física
    ctypes.windll.user32.keybd_event(vk_code, 0, 0, 0)  # Key down
    ctypes.windll.user32.keybd_event(vk_code, 0, 2, 0)  # Key up

    return f"Se ejecutó el comando de reproducción: {accion}."
