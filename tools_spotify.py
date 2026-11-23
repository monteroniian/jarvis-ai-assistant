# tools_spotify.py
import os
import re
import time
import random
import unicodedata
import threading
import subprocess
from difflib import SequenceMatcher
from dotenv import load_dotenv
import spotipy
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyOAuth

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Cargar variables del .env
load_dotenv(os.path.join(BASE_DIR, ".env"))

SPOTIPY_CLIENT_ID = os.getenv("SPOTIPY_CLIENT_ID")
SPOTIPY_CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET")
SPOTIPY_REDIRECT_URI = os.getenv("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8888/callback")

ALCANCE = [
    "user-modify-playback-state",
    "user-read-playback-state",
    "user-read-currently-playing",
    "playlist-read-private",
    "playlist-read-collaborative",
    "user-library-read",
    "user-read-recently-played",
    "user-top-read",
]

CACHE_PATH = os.path.join(BASE_DIR, ".spotify_cache")

auth_manager = SpotifyOAuth(
    client_id=SPOTIPY_CLIENT_ID,
    client_secret=SPOTIPY_CLIENT_SECRET,
    redirect_uri=SPOTIPY_REDIRECT_URI,
    scope=" ".join(ALCANCE),
    cache_path=CACHE_PATH,
    open_browser=True,
)

sp = spotipy.Spotify(auth_manager=auth_manager)

# --- REPRODUCTOR DE STREAMING DIRECTO DE BAJA LATENCIA ---
import warnings
with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import audioop

_ffmpeg_proc: subprocess.Popen | None = None
_stream_paused = False
_stream_stop = False
_stream_titulo_actual: str | None = None
_cuenta_spotify_free: bool | None = None
_musica_volumen_pct: int = 80
_dispositivo_cache_id: str | None = None
_dispositivo_cache_ts: float = 0.0
_playlists_cache: list[dict] = []
_playlists_cache_ts: float = 0.0


def _invalidar_cache_dispositivo():
    global _dispositivo_cache_id, _dispositivo_cache_ts
    _dispositivo_cache_id = None
    _dispositivo_cache_ts = 0.0


def ajustar_volumen_spotify(porcentaje: int | None = None, delta: int | None = None) -> str:
    """Ajusta exclusivamente el volumen de la música/Spotify (0 a 100%) sin tocar el volumen general de Windows ni la voz de Jarvis."""
    global _musica_volumen_pct

    if porcentaje is not None:
        nuevo_nivel = max(0, min(100, int(porcentaje)))
    elif delta is not None:
        nuevo_nivel = max(0, min(100, _musica_volumen_pct + int(delta)))
    else:
        nuevo_nivel = max(0, min(100, _musica_volumen_pct - 25))

    _musica_volumen_pct = nuevo_nivel
    escalar = nuevo_nivel / 100.0

    # 1. Si existe sesión de Spotify.exe en Windows WASAPI (Pycaw), ajustarla instantáneamente (~15ms)
    com_ok = False
    try:
        import comtypes
        from pycaw.pycaw import AudioUtilities

        comtypes.CoInitialize()
        com_ok = True
        for sesion in AudioUtilities.GetAllSessions():
            if sesion.Process and sesion.Process.name() and "spotify" in sesion.Process.name().lower():
                vol = sesion.SimpleAudioVolume
                vol.SetMute(0, None)
                vol.SetMasterVolume(escalar, None)
    except Exception:
        pass
    finally:
        if com_ok:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass

    # 2. Sincronizar Spotify API en segundo plano para no sumar 1-2s de latencia HTTP a la respuesta
    if _cuenta_spotify_free is not True and _tiene_token_cacheado():
        def _sync_vol():
            try:
                sp.volume(nuevo_nivel)
            except Exception:
                pass
        threading.Thread(target=_sync_vol, daemon=True).start()

    return f"Volumen de la música ajustado al {nuevo_nivel}%."


def detener_stream_local():
    global _ffmpeg_proc, _stream_stop, _stream_paused, _stream_titulo_actual
    _stream_stop = True
    _stream_paused = False
    _stream_titulo_actual = None
    if _ffmpeg_proc is not None:
        try:
            _ffmpeg_proc.terminate()
        except Exception:
            pass
        _ffmpeg_proc = None


def alternar_pausa_stream_local() -> bool:
    """Pausa o reanuda el stream local si está activo. Devuelve True si había un stream activo."""
    global _stream_paused
    if _ffmpeg_proc is not None and _ffmpeg_proc.poll() is None:
        _stream_paused = not _stream_paused
        return True
    return False


_STOPWORDS_MUSICA = {
    "de", "del", "la", "el", "los", "las", "un", "una", "unos", "unas",
    "por", "by", "feat", "ft", "con", "y", "and", "the", "a", "of", "in", "on",
}

_CONSULTAS_GENERICAS = {
    "musica", "cancion", "tema", "algo", "una musica", "un tema", "una cancion",
    "cualquiera", "playlist", "track", "audio",
}

_UMBRAL_RELEVANCIA_SPOTIFY = 0.52
_UMBRAL_RELEVANCIA_YOUTUBE = 0.55


def _normalizar_texto(texto: str) -> str:
    """Normaliza un texto a minúsculas sin acentos ni puntuación para comparar coincidencias."""
    sin_acentos = "".join(
        c for c in unicodedata.normalize("NFD", str(texto or "").lower())
        if unicodedata.category(c) != "Mn"
    )
    limpio = re.sub(r"[^a-z0-9]+", " ", sin_acentos)
    return re.sub(r"\s+", " ", limpio).strip()


def _tokens_relevantes(texto: str) -> list[str]:
    tokens = _normalizar_texto(texto).split()
    filtrados = [t for t in tokens if t not in _STOPWORDS_MUSICA]
    return filtrados or tokens


def _puntuar_texto_contra_consulta(consulta: str, titulo: str, contexto: str = "") -> float:
    """Devuelve un puntaje [0.0, 1.0+] midiendo cuánto coincide el candidato con la consulta."""
    consulta_norm = _normalizar_texto(consulta)
    titulo_norm = _normalizar_texto(titulo)
    completo_norm = f"{titulo_norm} {_normalizar_texto(contexto)}".strip()
    if not consulta_norm or not completo_norm:
        return 0.0

    tokens_q = _tokens_relevantes(consulta_norm)
    tokens_c = completo_norm.split()
    if not tokens_q or not tokens_c:
        return 0.0

    coincidencias = 0.0
    for tq in tokens_q:
        if tq in tokens_c:
            coincidencias += 1.0
        elif len(tq) >= 4 and any(tq in tc or tc in tq for tc in tokens_c if len(tc) >= 4):
            coincidencias += 0.85
        else:
            mejor_sim = max(SequenceMatcher(None, tq, tc).ratio() for tc in tokens_c)
            if mejor_sim >= 0.78:
                coincidencias += mejor_sim * 0.9

    cobertura = coincidencias / len(tokens_q)
    sim_titulo = SequenceMatcher(None, consulta_norm, titulo_norm).ratio()
    sim_completo = SequenceMatcher(None, consulta_norm, completo_norm).ratio()
    bonus_sub = 0.15 if (len(consulta_norm) >= 4 and (consulta_norm in titulo_norm or titulo_norm in consulta_norm)) else 0.0

    # Penaliza fuerte si faltan palabras clave en consultas de 2+ términos (ej. "Swiss Dream" vs "Swiss")
    if len(tokens_q) >= 2 and cobertura < 0.75:
        cobertura *= 0.65

    # Penaliza consultas de 1 sola palabra corta que no aparece como palabra real (ej. "Bro" vs "#BrooklynBloodPop!")
    if len(tokens_q) == 1 and len(tokens_q[0]) <= 4 and tokens_q[0] not in tokens_c:
        if sim_titulo < 0.75:
            return min(cobertura * 0.4, 0.35)

    return min(1.15, cobertura * 0.65 + max(sim_titulo, sim_completo) * 0.35 + bonus_sub)


def _seleccionar_mejor_youtube(consulta: str, entries: list[dict], permitir_largos: bool = False) -> tuple[dict | None, float]:
    """Elige la entrada de YouTube más fiel a la consulta entre las candidatas de ytsearch."""
    mejor_entry = None
    mejor_score = 0.0
    for idx, entry in enumerate(entries or []):
        if not entry or not entry.get("url"):
            continue
        duracion = entry.get("duration") or 0
        if not permitir_largos and duracion > 900:
            continue
        titulo = entry.get("title") or ""
        canal = entry.get("uploader") or entry.get("channel") or ""
        score = _puntuar_texto_contra_consulta(consulta, titulo, canal) - idx * 0.015
        if score > mejor_score:
            mejor_score = score
            mejor_entry = entry
    return mejor_entry, mejor_score


def _reproducir_stream_directo(consulta: str, etiqueta_mostrar: str, stream_url_precalculada: str | None = None):
    """Extrae el stream de audio con yt-dlp y lo reproduce en tiempo real vía ffmpeg + PyAudio."""
    global _ffmpeg_proc, _stream_stop, _stream_paused, _stream_titulo_actual

    detener_stream_local()
    _stream_titulo_actual = etiqueta_mostrar

    def _worker():
        global _ffmpeg_proc, _stream_stop, _stream_paused, _stream_titulo_actual
        import yt_dlp
        import imageio_ffmpeg
        import pyaudio

        try:
            stream_url = stream_url_precalculada
            if not stream_url:
                ydl_opts = {
                    "format": "bestaudio[ext=m4a]/bestaudio[acodec=opus]/bestaudio/best",
                    "noplaylist": True,
                    "quiet": True,
                    "no_warnings": True,
                    "default_search": "ytsearch5",
                    "socket_timeout": 6,
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(f"ytsearch5:{consulta}", download=False)
                    entries = info.get("entries") if isinstance(info, dict) else None
                    if entries:
                        mejor, _ = _seleccionar_mejor_youtube(consulta, entries)
                        elegido = mejor or entries[0]
                    else:
                        elegido = info
                    stream_url = elegido["url"]

            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            cmd = [
                ffmpeg_exe,
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5",
                "-fflags", "nobuffer",
                "-flags", "low_delay",
                "-i", stream_url,
                "-f", "s16le",
                "-acodec", "pcm_s16le",
                "-ar", "44100",
                "-ac", "2",
                "-loglevel", "quiet",
                "pipe:1",
            ]

            _stream_stop = False
            _stream_paused = False

            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=65536,
                creationflags=creationflags,
            )
            _ffmpeg_proc = proc

            pa = pyaudio.PyAudio()
            stream = pa.open(format=pyaudio.paInt16, channels=2, rate=44100, output=True)

            try:
                while not _stream_stop and proc.poll() is None:
                    if _stream_paused:
                        time.sleep(0.08)
                        continue
                    data = proc.stdout.read(4096)
                    if not data:
                        break
                    if _musica_volumen_pct != 100:
                        data = audioop.mul(data, 2, _musica_volumen_pct / 100.0)
                    stream.write(data)
            finally:
                stream.stop_stream()
                stream.close()
                pa.terminate()
                try:
                    proc.terminate()
                except Exception:
                    pass
                if _ffmpeg_proc is proc:
                    _ffmpeg_proc = None
                    _stream_titulo_actual = None
        except Exception as e:
            print(f"[Error stream directo]: {e}")
            _stream_titulo_actual = None

    threading.Thread(target=_worker, daemon=True).start()


def _tiene_token_cacheado() -> bool:
    if not os.path.exists(CACHE_PATH):
        return False
    try:
        return auth_manager.get_cached_token() is not None
    except Exception:
        return False


def _obtener_dispositivo_activo(forzar_refresco: bool = False):
    """Detecta y cachea el dispositivo activo de Spotify (TTL 120s) para ahorrar ~400ms por comando."""
    global _dispositivo_cache_id, _dispositivo_cache_ts
    ahora = time.monotonic()
    if not forzar_refresco and _dispositivo_cache_id and (ahora - _dispositivo_cache_ts) < 120.0:
        return _dispositivo_cache_id

    dispositivos = sp.devices().get("devices", [])
    if not dispositivos:
        _invalidar_cache_dispositivo()
        return None

    elegido = None
    for dev in dispositivos:
        if dev["is_active"]:
            elegido = dev["id"]
            break

    if not elegido:
        for dev in dispositivos:
            if dev["type"].lower() == "computer":
                elegido = dev["id"]
                break

    elegido = elegido or dispositivos[0]["id"]
    _dispositivo_cache_id = elegido
    _dispositivo_cache_ts = ahora
    return elegido


def _extraer_partes_consulta(busqueda: str) -> tuple[str, str | None, str | None]:
    """Limpia conectores coloquiales y detecta si viene en formato '<tema> de/por/by <artista>'."""
    texto = (busqueda or "").strip().strip(".,;¡!¿?\"'")
    texto = re.sub(
        r"^(?:la\s+canci[oó]n|el\s+tema|un\s+tema|una\s+canci[oó]n|m[uú]sica|algo)\s+(?:que\s+se\s+llama\s+|llamad[oa]\s+|de\s+)?",
        "",
        texto,
        flags=re.IGNORECASE,
    ).strip()
    m_de = re.match(r"^(.+?)\s+(?:de|por|by)\s+(.+)$", texto, flags=re.IGNORECASE)
    if m_de:
        parte1, parte2 = m_de.group(1).strip(), m_de.group(2).strip()
        if parte1 and parte2:
            return f"{parte1} {parte2}", parte1, parte2
    return texto, None, None


def _limpiar_consulta_spotify(busqueda: str) -> str:
    """Limpia conectores coloquiales ('de', 'por', 'el tema', 'la canción') para mejorar la precisión de búsqueda."""
    consulta, _, _ = _extraer_partes_consulta(busqueda)
    return consulta


def _limpiar_prefijo_playlist_o_radio(busqueda: str) -> str:
    texto = (busqueda or "").strip().strip(".,;¡!¿?\"'")
    texto = re.sub(
        r"^(?:mi\s+|la\s+|una\s+|el\s+)?(?:playlist|lista(?:\s+de\s+reproducci[oó]n)?|mix|radio)\s+(?:de\s+|llamad[oa]\s+)?",
        "",
        texto,
        flags=re.IGNORECASE,
    ).strip()
    return texto


def _puntuar_item_spotify(item: dict, tipo: str, consulta: str, titulo_hint: str | None = None, artista_hint: str | None = None) -> float:
    nombre = item.get("name") or ""
    artistas = " ".join(a.get("name", "") for a in (item.get("artists") or []) if a)
    if not artistas and tipo == "playlist":
        artistas = (item.get("owner") or {}).get("display_name") or ""

    score_general = _puntuar_texto_contra_consulta(consulta, nombre, artistas)
    if titulo_hint and artista_hint and artistas:
        score_titulo = _puntuar_texto_contra_consulta(titulo_hint, nombre)
        score_artista = _puntuar_texto_contra_consulta(artista_hint, artistas)
        if score_titulo >= 0.55 and score_artista >= 0.55:
            score_separado = score_titulo * 0.55 + score_artista * 0.45 + 0.1
            score_general = max(score_general, score_separado)
    return score_general


def _buscar_mejor_candidato_spotify(busqueda: str, tipo: str) -> tuple[dict | None, str, float]:
    """Ejecuta búsquedas en Spotify y elige el candidato más fiel a la consulta (no ciegamente items[0])."""
    consulta_limpia, titulo_hint, artista_hint = _extraer_partes_consulta(busqueda)
    consultas: list[str] = [consulta_limpia]
    if titulo_hint and artista_hint and tipo == "track":
        consultas.append(f"track:{titulo_hint} artist:{artista_hint}")
    busqueda_raw = busqueda.strip().strip(".,;¡!¿?\"'")
    if busqueda_raw and _normalizar_texto(busqueda_raw) != _normalizar_texto(consulta_limpia):
        consultas.append(busqueda_raw)

    candidatos: dict[str, tuple[int, dict]] = {}
    orden = 0
    for q_idx, q in enumerate(consultas):
        try:
            res = sp.search(q=q, type=tipo, limit=10)
            for item in (res.get(f"{tipo}s") or {}).get("items", []):
                if not item:
                    continue
                uri = item.get("uri")
                if uri and uri not in candidatos:
                    candidatos[uri] = (orden, item)
                    orden += 1
            if q_idx == 0 and candidatos:
                top_score = max(
                    _puntuar_item_spotify(it, tipo, consulta_limpia, titulo_hint, artista_hint) - idx * 0.008
                    for idx, it in candidatos.values()
                )
                if top_score >= 0.78:
                    break
        except Exception:
            continue

    tipo_final = tipo
    if not candidatos and tipo != "track":
        try:
            res = sp.search(q=consulta_limpia, type="track", limit=10)
            for item in (res.get("tracks") or {}).get("items", []):
                if not item:
                    continue
                uri = item.get("uri")
                if uri and uri not in candidatos:
                    candidatos[uri] = (orden, item)
                    orden += 1
            if candidatos:
                tipo_final = "track"
        except Exception:
            pass

    mejor_item = None
    mejor_score = 0.0
    for idx, item in candidatos.values():
        score = _puntuar_item_spotify(item, tipo_final, consulta_limpia, titulo_hint, artista_hint) - idx * 0.008
        if score > mejor_score:
            mejor_score = score
            mejor_item = item

    return mejor_item, tipo_final, mejor_score


def _obtener_playlists_usuario(max_items: int = 150) -> list[dict]:
    """Obtiene y cachea (TTL 60s) las playlists guardadas, privadas y colaborativas de la cuenta vinculada."""
    global _playlists_cache, _playlists_cache_ts
    ahora = time.monotonic()
    if _playlists_cache and (ahora - _playlists_cache_ts) < 60.0:
        return _playlists_cache[:max_items]

    playlists: list[dict] = []
    offset = 0
    while len(playlists) < max_items:
        lote = min(50, max_items - len(playlists))
        res = sp.current_user_playlists(limit=lote, offset=offset)
        items = (res or {}).get("items") or []
        for pl in items:
            if pl and pl.get("uri"):
                playlists.append(pl)
        if not res or not res.get("next") or not items:
            break
        offset += len(items)
    if playlists:
        _playlists_cache = playlists
        _playlists_cache_ts = ahora
    return playlists


def _buscar_en_mis_playlists(consulta: str, preferir_radio: bool = False) -> tuple[dict | None, float]:
    """Busca primero dentro de las playlists personales/seguidas del usuario."""
    try:
        playlists = _obtener_playlists_usuario()
    except Exception:
        return None, 0.0

    consulta_limpia = _limpiar_prefijo_playlist_o_radio(consulta) or consulta
    mejor_pl = None
    mejor_score = 0.0
    for pl in playlists:
        nombre = pl.get("name") or ""
        owner = (pl.get("owner") or {}).get("display_name") or ""
        score = _puntuar_texto_contra_consulta(consulta_limpia, nombre, owner)
        if preferir_radio and ("radio" in _normalizar_texto(nombre) or "mix" in _normalizar_texto(nombre)):
            score += 0.1
        if score > mejor_score:
            mejor_score = score
            mejor_pl = pl
    return mejor_pl, mejor_score


def _detectar_modo_biblioteca(consulta_norm: str) -> str | None:
    """Detecta pedidos de biblioteca personal ('mis me gusta', 'lo que más escucho', 'recientes')."""
    if any(p in consulta_norm for p in ("me gusta", "favorit", "guardad", "mi musica")):
        return "saved"
    if any(p in consulta_norm for p in ("mas escuch", "escucho siempre", "mis temas", "mi mix")):
        return "top"
    if any(p in consulta_norm for p in ("ultimo que escuche", "reciente", "historial")):
        return "recent"
    return None


def _construir_uris_biblioteca(modo: str) -> list[tuple[str, str]]:
    """Devuelve lista de (uri, 'Titulo Artista') desde la biblioteca personal del usuario."""
    pistas: list[tuple[str, str]] = []
    vistos: set[str] = set()

    def _agregar_track(t: dict | None):
        if not t or not t.get("uri") or t["uri"] in vistos:
            return
        vistos.add(t["uri"])
        art = t["artists"][0]["name"] if t.get("artists") else ""
        pistas.append((t["uri"], f"{t.get('name', '')} {art}".strip()))

    if modo == "saved":
        res = sp.current_user_saved_tracks(limit=50)
        for entry in (res or {}).get("items", []):
            _agregar_track((entry or {}).get("track"))
        if len(pistas) > 1:
            random.shuffle(pistas)
    elif modo == "recent":
        res = sp.current_user_recently_played(limit=30)
        for entry in (res or {}).get("items", []):
            _agregar_track((entry or {}).get("track"))
    elif modo == "top":
        res = sp.current_user_top_tracks(limit=30, time_range="medium_term")
        for t in (res or {}).get("items", []):
            _agregar_track(t)
        if len(pistas) > 1:
            random.shuffle(pistas)
    elif modo == "personal_radio":
        try:
            top_artists = sp.current_user_top_artists(limit=5, time_range="medium_term").get("items", [])
            for art in top_artists:
                if art and art.get("id"):
                    for t in sp.artist_top_tracks(art["id"]).get("tracks", [])[:4]:
                        _agregar_track(t)
        except Exception:
            pass
        try:
            for t in sp.current_user_top_tracks(limit=15, time_range="short_term").get("items", []):
                _agregar_track(t)
        except Exception:
            pass
        if len(pistas) > 1:
            random.shuffle(pistas)

    return pistas[:40]


def _construir_radio_de_consulta(consulta: str) -> tuple[dict | None, list[tuple[str, str]], str]:
    """Resuelve una radio como playlist de Spotify Radio o cola inteligente de pistas afines."""
    tema = _limpiar_prefijo_playlist_o_radio(consulta)
    tema_norm = _normalizar_texto(tema)

    # 1. Radio personal del usuario ("mi radio", "radio", "una radio")
    if not tema_norm or tema_norm in ("mi", "mia", "personal", "spotify", "para mi"):
        pistas = _construir_uris_biblioteca("personal_radio")
        return None, pistas, "tu radio personalizada de Spotify"

    # 2. Buscar primero si el usuario tiene esa radio o mix guardado en sus playlists
    mi_pl, mi_score = _buscar_en_mis_playlists(f"{tema} radio", preferir_radio=True)
    if mi_pl and mi_score >= 0.62:
        return mi_pl, [], f"'{mi_pl['name']}'"

    # 3. Buscar playlist pública "<tema> Radio" en Spotify (priorizando playlists oficiales o con "Radio" en el título)
    mejor_pl = None
    mejor_score = 0.0
    try:
        res = sp.search(q=f"{tema} Radio", type="playlist", limit=10)
        for idx, pl in enumerate((res.get("playlists") or {}).get("items", [])):
            if not pl or not pl.get("uri"):
                continue
            nombre = pl.get("name") or ""
            owner_id = ((pl.get("owner") or {}).get("id") or "").lower()
            score = _puntuar_texto_contra_consulta(tema, nombre) - idx * 0.01
            if "radio" in _normalizar_texto(nombre):
                score += 0.12
            if owner_id == "spotify":
                score += 0.15
            if score > mejor_score:
                mejor_score = score
                mejor_pl = pl
    except Exception:
        pass

    if mejor_pl and mejor_score >= 0.58:
        return mejor_pl, [], f"la radio '{mejor_pl['name']}'"

    # 4. Armar radio al vuelo con top tracks del artista o pistas que coincidan con el tema/género
    pistas: list[tuple[str, str]] = []
    vistos: set[str] = set()
    try:
        art_res = sp.search(q=tema, type="artist", limit=5).get("artists", {}).get("items", [])
        mejor_art = None
        mejor_art_score = 0.0
        for art in art_res:
            if not art:
                continue
            sc = _puntuar_texto_contra_consulta(tema, art.get("name") or "")
            if sc > mejor_art_score:
                mejor_art_score = sc
                mejor_art = art
        if mejor_art and mejor_art_score >= 0.72:
            for t in sp.artist_top_tracks(mejor_art["id"]).get("tracks", []):
                if t and t.get("uri") and t["uri"] not in vistos:
                    vistos.add(t["uri"])
                    art_n = t["artists"][0]["name"] if t.get("artists") else mejor_art["name"]
                    pistas.append((t["uri"], f"{t['name']} {art_n}".strip()))
            for t in sp.search(q=f"artist:{mejor_art['name']}", type="track", limit=15).get("tracks", {}).get("items", []):
                if t and t.get("uri") and t["uri"] not in vistos:
                    vistos.add(t["uri"])
                    art_n = t["artists"][0]["name"] if t.get("artists") else mejor_art["name"]
                    pistas.append((t["uri"], f"{t['name']} {art_n}".strip()))
            if len(pistas) > 1:
                random.shuffle(pistas)
            return None, pistas[:30], f"la radio de {mejor_art['name']}"
    except Exception:
        pass

    try:
        for t in sp.search(q=tema, type="track", limit=20).get("tracks", {}).get("items", []):
            if t and t.get("uri") and t["uri"] not in vistos:
                vistos.add(t["uri"])
                art_n = t["artists"][0]["name"] if t.get("artists") else ""
                pistas.append((t["uri"], f"{t['name']} {art_n}".strip()))
    except Exception:
        pass

    if pistas:
        random.shuffle(pistas)
    return None, pistas[:30], f"la radio de '{tema}'"


def _intentar_stream_youtube_validado(consulta: str, etiqueta: str) -> bool:
    """Busca en YouTube y reproduce solo si el título encontrado coincide realmente con la consulta."""
    import yt_dlp

    ydl_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio[acodec=opus]/bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch5",
        "socket_timeout": 6,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch5:{consulta}", download=False)
        entries = info.get("entries") if isinstance(info, dict) else None
        if entries:
            mejor, score = _seleccionar_mejor_youtube(consulta, entries)
            if mejor and score >= _UMBRAL_RELEVANCIA_YOUTUBE:
                _reproducir_stream_directo(consulta, etiqueta, stream_url_precalculada=mejor["url"])
                return True
        elif isinstance(info, dict) and info.get("url"):
            score = _puntuar_texto_contra_consulta(consulta, info.get("title") or "")
            if score >= _UMBRAL_RELEVANCIA_YOUTUBE:
                _reproducir_stream_directo(consulta, etiqueta, stream_url_precalculada=info["url"])
                return True
    except Exception as e:
        print(f"[Error buscando en YouTube]: {e}")
    return False


def _resolver_consulta_audio_fallback(item: dict, tipo: str, nombre: str, artista: str) -> str:
    """Obtiene una consulta de pista real cuando tipo es artist/album/playlist y cae a stream local."""
    if tipo == "artist" and item.get("id"):
        try:
            top = sp.artist_top_tracks(item["id"]).get("tracks", [])
            if top:
                t = top[0]
                t_art = t["artists"][0]["name"] if t.get("artists") else nombre
                return f"{t['name']} {t_art}".strip()
        except Exception:
            pass
    elif tipo == "album" and item.get("id"):
        try:
            tracks = sp.album_tracks(item["id"], limit=1).get("items", [])
            if tracks:
                return f"{tracks[0]['name']} {artista or nombre}".strip()
        except Exception:
            pass
    elif tipo == "playlist" and item.get("id"):
        try:
            pl_items = sp.playlist_items(item["id"], limit=5).get("items", [])
            for entry in pl_items:
                t = (entry or {}).get("track")
                if t and t.get("name"):
                    t_art = t["artists"][0]["name"] if t.get("artists") else ""
                    return f"{t['name']} {t_art}".strip()
        except Exception:
            pass
    return f"{nombre} {artista}".strip()


def _reproducir_en_spotify_o_stream(
    *,
    uris: list[str] | None = None,
    context_uri: str | None = None,
    fallback_consulta: str | None = None,
    fallback_resolver=None,
    etiqueta: str,
    verificar_track_uri: str | None = None,
) -> bool:
    """Intenta reproducir en Spotify Connect y cae a stream directo solo si no es posible."""
    global _cuenta_spotify_free
    reprodujo_en_spotify = False

    if _cuenta_spotify_free is not True:
        for intento_dev in range(2):
            device_id = _obtener_dispositivo_activo(forzar_refresco=(intento_dev > 0))
            if not device_id:
                break
            try:
                if uris:
                    sp.start_playback(device_id=device_id, uris=uris, offset={"position": 0})
                elif context_uri:
                    sp.start_playback(device_id=device_id, context_uri=context_uri)

                _cuenta_spotify_free = False
                reprodujo_en_spotify = True
                detener_stream_local()

                # Verificación post-play en segundo plano para responder sin sumar ~1.5-2.5s de delay
                if verificar_track_uri:
                    def _verificar_en_bg(dev_id=device_id, track_uri=verificar_track_uri, lista_uris=uris):
                        try:
                            cambio_confirmado = False
                            actual = None
                            for _ in range(2):
                                time.sleep(0.25)
                                actual = sp.current_playback()
                                if actual and (actual.get("item") or {}).get("uri") == track_uri:
                                    cambio_confirmado = True
                                    break
                            if not cambio_confirmado and lista_uris:
                                sp.start_playback(device_id=dev_id, uris=lista_uris, offset={"position": 0})
                                time.sleep(0.25)
                                actual = sp.current_playback()
                            if actual and lista_uris and len(lista_uris) == 1 and actual.get("repeat_state") != "off":
                                sp.repeat("off", device_id=dev_id)
                        except Exception:
                            pass
                    threading.Thread(target=_verificar_en_bg, daemon=True).start()
                break
            except SpotifyException as se:
                msg_err = str(se).lower()
                if se.http_status == 403 and ("premium" in msg_err or "restriction" in msg_err):
                    _cuenta_spotify_free = True
                    break
                if se.http_status == 404 and intento_dev == 0:
                    _invalidar_cache_dispositivo()
                    continue
                break
            except Exception:
                _invalidar_cache_dispositivo()
                break

    if not reprodujo_en_spotify:
        consulta_fb = fallback_consulta if fallback_consulta else (fallback_resolver() if fallback_resolver else "")
        if consulta_fb:
            _reproducir_stream_directo(consulta_fb, etiqueta)
    return reprodujo_en_spotify


def controlar_spotify_api(accion: str) -> bool:
    """Controla directamente la reproducción en Spotify vía API (pausa real sin depender de teclas multimedia)."""
    global _cuenta_spotify_free
    if _cuenta_spotify_free is True or not _tiene_token_cacheado():
        return False

    accion_norm = (accion or "").lower().strip()
    try:
        # Atajo directo sin consultar current_playback() para acciones unívocas
        if accion_norm in ("siguiente", "next"):
            device_id = _obtener_dispositivo_activo()
            sp.next_track(device_id=device_id)
            return True
        if accion_norm in ("anterior", "prev"):
            device_id = _obtener_dispositivo_activo()
            sp.previous_track(device_id=device_id)
            return True

        actual = sp.current_playback()
        device_id = ((actual or {}).get("device") or {}).get("id") or _obtener_dispositivo_activo()
        esta_sonando = bool(actual and actual.get("is_playing"))

        if accion_norm in ("pausa", "detener", "stop"):
            if esta_sonando:
                sp.pause_playback(device_id=device_id)
            return True
        if accion_norm == "play":
            if not esta_sonando and device_id:
                sp.start_playback(device_id=device_id)
            return True
        if accion_norm == "play_pause":
            if esta_sonando:
                sp.pause_playback(device_id=device_id)
            elif device_id:
                sp.start_playback(device_id=device_id)
            return True
    except SpotifyException as se:
        msg_err = str(se).lower()
        if se.http_status == 403 and ("premium" in msg_err or "restriction" in msg_err):
            _cuenta_spotify_free = True
        elif se.http_status == 404:
            _invalidar_cache_dispositivo()
    except Exception:
        pass
    return False


def listar_playlists_spotify() -> str:
    """Devuelve la lista de playlists personales y guardadas en la cuenta de Spotify conectada."""
    try:
        playlists = _obtener_playlists_usuario(max_items=25)
        if not playlists:
            return "No encontré playlists guardadas en tu cuenta de Spotify."
        resumen = []
        for pl in playlists[:12]:
            nombre = pl.get("name") or "Sin nombre"
            total = (pl.get("tracks") or {}).get("total")
            resumen.append(f"{nombre} ({total} temas)" if total is not None else nombre)
        sufijo = f" (mostrando 12 de {len(playlists)})" if len(playlists) > 12 else ""
        return f"Tus playlists en Spotify{sufijo}: " + ", ".join(resumen) + "."
    except Exception as e:
        return f"No pude leer tus playlists de Spotify: {str(e)}"


def reproducir_musica(busqueda: str, tipo: str = "track") -> str:
    """Busca y reproduce canciones, artistas, álbumes, playlists personales/públicas o radios en Spotify."""
    tipo_norm = tipo if tipo in ("track", "artist", "album", "playlist", "radio") else "track"
    consulta_raw = (busqueda or "").strip()
    consulta_norm = _normalizar_texto(consulta_raw)

    if not consulta_norm or len(consulta_norm) < 2:
        return "No entendí qué canción, playlist o radio querés reproducir. ¿Me repetís el nombre?"

    # Detectar pedidos de biblioteca personal ("mis me gusta", "lo que más escucho", "recientes")
    modo_bib = _detectar_modo_biblioteca(consulta_norm)
    if modo_bib:
        try:
            pistas = _construir_uris_biblioteca(modo_bib)
            if not pistas:
                return "No encontré canciones en esa sección de tu biblioteca de Spotify."
            etiquetas_modo = {
                "saved": "tus 'Me gusta' de Spotify",
                "recent": "lo último que escuchaste en Spotify",
                "top": "tus canciones más escuchadas",
            }
            etiqueta = etiquetas_modo.get(modo_bib, "tu música de Spotify")
            _reproducir_en_spotify_o_stream(
                uris=[u for u, _ in pistas],
                fallback_consulta=pistas[0][1],
                etiqueta=etiqueta,
            )
            return f"Reproduciendo {etiqueta}."
        except Exception as e:
            return f"No pude acceder a tu biblioteca de Spotify: {str(e)}"

    # Pedidos genéricos tipo "mix aleatorio" / "playlist aleatoria" -> radio personal o playlist propia al azar
    if any(p in consulta_norm for p in ("mix aleatorio", "playlist aleatoria", "algo aleatorio", "al azar")):
        try:
            pistas = _construir_uris_biblioteca("personal_radio")
            if pistas:
                _reproducir_en_spotify_o_stream(
                    uris=[u for u, _ in pistas],
                    fallback_consulta=pistas[0][1],
                    etiqueta="un mix personalizado de Spotify",
                )
                return "Reproduciendo un mix personalizado de Spotify."
        except Exception:
            pass

    # Detectar radios explícitas ("radio de Drake", "mi radio") o tipo == "radio"
    if tipo_norm == "radio" or re.match(r"^(?:la\s+|una\s+|mi\s+)?radio\b", consulta_raw, flags=re.IGNORECASE):
        try:
            pl_radio, pistas_radio, etiqueta = _construir_radio_de_consulta(consulta_raw)
            if pl_radio:
                _reproducir_en_spotify_o_stream(
                    context_uri=pl_radio["uri"],
                    fallback_resolver=lambda: _resolver_consulta_audio_fallback(pl_radio, "playlist", pl_radio.get("name", ""), ""),
                    etiqueta=etiqueta,
                )
                return f"Reproduciendo {etiqueta}."
            if pistas_radio:
                _reproducir_en_spotify_o_stream(
                    uris=[u for u, _ in pistas_radio],
                    fallback_consulta=pistas_radio[0][1],
                    etiqueta=etiqueta,
                )
                return f"Reproduciendo {etiqueta}."
            return f"No encontré ninguna radio para '{busqueda}'."
        except Exception as e:
            return f"No pude iniciar la radio solicitada: {str(e)}"

    # Si piden una playlist (explícitamente o por phrasing tipo "mi playlist ...")
    es_pedido_playlist = tipo_norm == "playlist" or bool(
        re.match(r"^(?:mi\s+|la\s+|una\s+)?(?:playlist|lista(?:\s+de\s+reproducci[oó]n)?)\b", consulta_raw, flags=re.IGNORECASE)
    )
    if es_pedido_playlist:
        tipo_norm = "playlist"
        consulta_pl = _limpiar_prefijo_playlist_o_radio(consulta_raw)
        if not consulta_pl:
            return listar_playlists_spotify()
        mi_pl, mi_score = _buscar_en_mis_playlists(consulta_pl)
        if mi_pl and mi_score >= 0.50:
            etiqueta = f"tu playlist '{mi_pl['name']}'"
            _reproducir_en_spotify_o_stream(
                context_uri=mi_pl["uri"],
                fallback_resolver=lambda: _resolver_consulta_audio_fallback(mi_pl, "playlist", mi_pl.get("name", ""), ""),
                etiqueta=etiqueta,
            )
            return f"Reproduciendo {etiqueta}."
        busqueda = consulta_pl

    consulta_limpia = _limpiar_consulta_spotify(busqueda) or consulta_raw
    consulta_limpia_norm = _normalizar_texto(consulta_limpia)
    if not consulta_limpia_norm or len(consulta_limpia_norm) < 2 or consulta_limpia_norm in _CONSULTAS_GENERICAS:
        return "No entendí qué canción o artista querés reproducir. ¿Me repetís el nombre?"

    try:
        item, tipo_final, score = _buscar_mejor_candidato_spotify(busqueda, tipo_norm)

        if not item or score < _UMBRAL_RELEVANCIA_SPOTIFY:
            tokens_consulta = _tokens_relevantes(consulta_limpia_norm)
            es_consulta_suficiente = len(tokens_consulta) >= 2 or len(consulta_limpia_norm) >= 5
            if es_consulta_suficiente and _intentar_stream_youtube_validado(consulta_limpia, f"'{consulta_limpia}'"):
                return f"Reproduciendo '{consulta_limpia}'."
            return f"No encontré una canción que coincida claramente con '{busqueda}'. ¿Me repetís el nombre o el artista?"

        nombre = item["name"]
        artista = item["artists"][0]["name"] if item.get("artists") else ""
        etiqueta = f"'{nombre}' de {artista}" if artista else f"'{nombre}'"

        _reproducir_en_spotify_o_stream(
            uris=[item["uri"]] if tipo_final == "track" else None,
            context_uri=None if tipo_final == "track" else item["uri"],
            fallback_resolver=lambda: _resolver_consulta_audio_fallback(item, tipo_final, nombre, artista),
            etiqueta=etiqueta,
            verificar_track_uri=item["uri"] if tipo_final == "track" else None,
        )
        return f"Reproduciendo {etiqueta}."

    except Exception:
        if len(consulta_limpia_norm) >= 3 and _intentar_stream_youtube_validado(consulta_limpia, f"'{consulta_limpia}'"):
            return f"Reproduciendo '{consulta_limpia}'."
        return f"No encontré una canción que coincida claramente con '{busqueda}'."


def obtener_cancion_actual(interactivo: bool = True) -> str:
    """Devuelve el nombre de la canción y artista que está sonando actualmente."""
    if _stream_titulo_actual:
        estado = " (Pausado)" if _stream_paused else ""
        return f"Actualmente está sonando {_stream_titulo_actual}{estado}."

    if _cuenta_spotify_free is True:
        return "No hay ninguna pista en reproducción en este momento."

    if not interactivo and not _tiene_token_cacheado():
        return "Spotify listo (esperando primera vinculación)"

    try:
        actual = sp.current_playback()
        if actual and actual.get("is_playing") and actual.get("item"):
            tema = actual["item"]["name"]
            artista = actual["item"]["artists"][0]["name"]
            return f"Actualmente está sonando '{tema}' de {artista}."
        return "No hay ninguna pista en reproducción en este momento."
    except Exception as e:
        return f"No se pudo consultar el estado de reproducción: {str(e)}"
