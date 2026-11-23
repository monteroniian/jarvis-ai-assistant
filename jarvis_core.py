# jarvis_core.py
from tools_windows import (
    obtener_estado_hardware,
    lanzar_aplicacion,
    bloquear_estacion_trabajo,
    silenciar_volumen,
    DECLARACIONES_HERRAMIENTAS,
    MAPA_HERRAMIENTAS,
)
from tools_multimedia import ajustar_volumen, controlar_reproduccion
from tools_spotify import reproducir_musica, obtener_cancion_actual, listar_playlists_spotify
from jarvis_voice import main, hablar, escuchar_microfono, procesar_orden_gemini

LISTA_HERRAMIENTAS = [
    obtener_estado_hardware,
    lanzar_aplicacion,
    bloquear_estacion_trabajo,
    silenciar_volumen,
    ajustar_volumen,
    controlar_reproduccion,
    reproducir_musica,
    obtener_cancion_actual,
    listar_playlists_spotify,
]

if __name__ == "__main__":
    main()
