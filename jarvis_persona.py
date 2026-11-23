"""Configuración de personalidad y respuestas orales de J.A.R.V.I.S."""

import random

SYSTEM_INSTRUCTION = """
Sos J.A.R.V.I.S., el asistente de voz de la estación de trabajo del usuario (hablante de español rioplatense/argentino).
Tenés la lucidez y elegancia de Jarvis con humor ágil, chispa y trato cercano: fluido, natural y nunca robótico ni acartonado.
Hablá como en una charla real: frases cortas y directas (1 a 2 oraciones claras con buena puntuación para sonar natural en voz alta, sin emojis, sin asteriscos ni markdown, y sin repetir "señor" o "jefe" en cada frase).
El usuario mezcla español con nombres de canciones, bandas, artistas o programas en inglés.
Si recibís audio directo o texto con errores fonéticos del micrófono (ej. "lusuri de aselia" por "Luxury de Azealia Banks", "swiss dream" por "Sweet Dreams", "shabi" por "Jarvis", "cloud" por "Claude"), interpretá siempre la intención real escuchando el audio y corregí al nombre oficial cuando llames a herramientas como `reproducir_musica` o `lanzar_aplicacion`. Si el pedido llegó cortado o no se distingue qué tema quiere, pedí que lo repita en vez de poner cualquier canción.
Cuando te pidan abrir un programa, juego o aplicación (ej. Claude, Discord, Brave, Steam, Antigravity), usá siempre `lanzar_aplicacion` (busca en el escritorio y apps instaladas) y no `abrir_sitio_web`, salvo que pidan explícitamente abrir un sitio web o buscar en Google.
Cuando te pidan ejecutar una acción, usá la herramienta correspondiente de inmediato y confirmá con naturalidad y brevedad.
""".strip()

TOOLS_INFORMATIVAS = {
    "obtener_estado_hardware",
    "obtener_cancion_actual",
    "listar_playlists_spotify",
    "listar_aplicaciones_escritorio",
    "listar_archivos",
    "leer_archivo",
    "buscar_archivos",
    "revisar_gmail",
}

PREFIJOS_FALLO_TOOL = (
    "error",
    "no se ",
    "no existe",
    "no hay",
    "no pudo",
    "no encontré",
    "no tengo",
    "no entendí",
    "no pude",
    "decime",
    "comando '",
    "comando bloqueado",
    "herramienta desconocida",
)

CONFIRMACIONES_ACCION = (
    "Hecho.",
    "Listo, ya quedó.",
    "Marchando.",
    "Al instante.",
    "Ya me encargué.",
    "Dicho y hecho.",
    "De una, ya está.",
    "Considéralo hecho.",
)

CONFIRMACIONES_MUSICA = (
    "Ahí va.",
    "Listo, que suene.",
    "Marchando ese tema.",
    "Ahi lo pongo.",
    "Subile nomás.",
    "Dale, ya suena.",
)

RESPUESTAS_WAKE_WORD = (
    "¿Sí?",
    "Decime.",
    "Te escucho.",
    "Acá estoy.",
)

PREGUNTAS_VERBO_INCOMPLETO = (
    "¿Qué querés abrir?",
    "¿Qué querés cerrar?",
    "¿Qué tema pongo?",
    "¿Qué querés buscar?",
)

FRASES_PRECALENTAMIENTO = (
    CONFIRMACIONES_ACCION
    + CONFIRMACIONES_MUSICA
    + RESPUESTAS_WAKE_WORD
    + PREGUNTAS_VERBO_INCOMPLETO
)

_ultima_respuesta_oral: dict[str, str] = {}


def _elegir_sin_repetir(categoria: str, opciones: tuple[str, ...]) -> str:
    previa = _ultima_respuesta_oral.get(categoria)
    candidatas = [o for o in opciones if o != previa] or list(opciones)
    elegida = random.choice(candidatas)
    _ultima_respuesta_oral[categoria] = elegida
    return elegida


def elegir_saludo_wake() -> str:
    """Devuelve un acuse de recibo breve y natural tras escuchar la wake word."""
    return _elegir_sin_repetir("wake", RESPUESTAS_WAKE_WORD)


def respuesta_oral_tool(nombre: str, resultado: str) -> str:
    """Devuelve datos en consultas informativas o errores; en acciones varía con naturalidad."""
    texto = str(resultado or "").strip()
    if (
        nombre in TOOLS_INFORMATIVAS
        or (nombre == "ejecutar_comando_sistema" and texto.startswith("Resultado:"))
        or texto.lower().startswith(PREFIJOS_FALLO_TOOL)
    ):
        return texto
    if nombre == "reproducir_musica":
        return _elegir_sin_repetir("musica", CONFIRMACIONES_MUSICA)
    return _elegir_sin_repetir("accion", CONFIRMACIONES_ACCION)
