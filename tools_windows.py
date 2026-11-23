# tools_windows.py
import os
import re
import json
import time
import shutil
import difflib
import subprocess
import ctypes
import threading
import unicodedata
import webbrowser
from urllib.parse import quote_plus
import psutil
from tools_multimedia import ajustar_volumen, controlar_reproduccion
from tools_spotify import (
    reproducir_musica,
    obtener_cancion_actual,
    ajustar_volumen_spotify,
    listar_playlists_spotify,
)
from tools_gmail import revisar_gmail

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HOME_DIR = os.path.expanduser("~")
ONEDRIVE_DIR = os.path.join(HOME_DIR, "OneDrive")


def _resolver_ruta_base(ruta: str) -> str:
    """Resuelve alias comunes ('escritorio', 'descargas', 'documentos', 'jarvis') o rutas relativas/absolutas."""
    if not ruta:
        return BASE_DIR

    limpia = ruta.strip().strip('"').strip("'")
    baja = limpia.lower()

    escritorio = (
        os.path.join(ONEDRIVE_DIR, "Desktop")
        if os.path.exists(os.path.join(ONEDRIVE_DIR, "Desktop"))
        else os.path.join(HOME_DIR, "Desktop")
    )
    documentos = (
        os.path.join(ONEDRIVE_DIR, "Documents")
        if os.path.exists(os.path.join(ONEDRIVE_DIR, "Documents"))
        else os.path.join(HOME_DIR, "Documents")
    )
    descargas = os.path.join(HOME_DIR, "Downloads")

    mapa_alias = {
        "escritorio": escritorio,
        "desktop": escritorio,
        "descargas": descargas,
        "downloads": descargas,
        "documentos": documentos,
        "documents": documentos,
        "jarvis": BASE_DIR,
        "mi carpeta": BASE_DIR,
        "carpeta jarvis": BASE_DIR,
        "home": HOME_DIR,
        "usuario": HOME_DIR,
    }

    if baja in mapa_alias:
        return mapa_alias[baja]

    for alias, destino in mapa_alias.items():
        if baja.startswith(alias + "/") or baja.startswith(alias + "\\"):
            sub = limpia[len(alias) + 1 :]
            return os.path.join(destino, sub)

    if os.path.isabs(limpia):
        return limpia

    return os.path.join(BASE_DIR, limpia)


def obtener_estado_hardware() -> str:
    """Devuelve el uso actual de CPU, memoria RAM y porcentaje de disco."""
    cpu_usage = psutil.cpu_percent(interval=0.2)
    ram = psutil.virtual_memory()
    disco = psutil.disk_usage("/")
    return (
        f"CPU al {cpu_usage}%. "
        f"RAM en uso: {ram.percent}% ({round(ram.used / (1024**3), 1)}GB de {round(ram.total / (1024**3), 1)}GB). "
        f"Almacenamiento ocupado: {disco.percent}%."
    )


_EXTENSIONES_LANZABLES = (".lnk", ".url", ".appref-ms", ".exe")
_PALABRAS_EXCLUIDAS_APP = (
    "uninstall", "desinstalar", "uninstaller", "setup", "installer",
    "bug report", "user manual", "release notes", "actualizacion",
    "actualización", "error reporter", "kernelmodule", "whql",
)
_ALIAS_APPS_ESCRITORIO = {
    "cloud": "claude",
    "claud": "claude",
    "clod": "claude",
    "claude desktop": "claude",
    "cloud desktop": "claude",
    "discor": "discord",
    "discors": "discord",
    "anti gravity": "antigravity",
    "antigraviti": "antigravity",
    "vscode": "visual studio code",
    "vs code": "visual studio code",
    "code": "visual studio code",
    "cura": "ultimaker cura",
    "ultimaker": "ultimaker cura",
    "counter": "counter strike 2",
    "counter strike": "counter strike 2",
    "cs2": "counter strike 2",
    "cs 2": "counter strike 2",
    "rocket": "rocket league",
    "epic": "epic games launcher",
    "epic games": "epic games launcher",
    "virtualbox": "oracle virtualbox",
    "virtual box": "oracle virtualbox",
    "g hub": "logitech g hub",
    "logitech": "logitech g hub",
    "obs": "obs studio",
    "utorrent": "utorrent web",
    "fc 26": "ea sports fc 26",
    "fc26": "ea sports fc 26",
    "fifa": "ea sports fc 26",
    "ea app": "ea",
    "minecraft": "tlauncher",
    "roblox": "roblox studio",
    "redragon": "redragon gaming headset driver",
    "wallpaper": "wallpaper engine",
    "bloc de notas": "notepad",
    "configuracion": "configuracion",
    "ajustes": "configuracion",
}
_APPS_SISTEMA = {
    "code": "code",
    "vscode": "code",
    "terminal": "wt.exe",
    "windows terminal": "wt.exe",
    "cmd": "cmd.exe",
    "powershell": "powershell.exe",
    "chrome": "start chrome",
    "google chrome": "start chrome",
    "edge": "start msedge",
    "firefox": "start firefox",
    "spotify": "start spotify",
    "explorador": "explorer.exe",
    "archivos": "explorer.exe",
    "calculadora": "calc.exe",
    "calc": "calc.exe",
    "bloc de notas": "notepad.exe",
    "notepad": "notepad.exe",
    "paint": "mspaint.exe",
    "word": "start winword",
    "excel": "start excel",
    "powerpoint": "start powerpnt",
    "configuracion": "start ms-settings:",
    "ajustes": "start ms-settings:",
    "whatsapp": "start whatsapp:",
}

_start_apps_lock = threading.Lock()
_start_apps_cache: list[tuple[str, str]] = []
_start_apps_timestamp: float = 0.0


def _normalizar_nombre_app(texto: str) -> str:
    """Normaliza nombres de apps quitando tildes, extensiones y símbolos para comparar de forma robusta."""
    t = unicodedata.normalize("NFKD", str(texto or ""))
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    for ext in _EXTENSIONES_LANZABLES:
        if t.endswith(ext):
            t = t[: -len(ext)]
            break
    t = re.sub(r"[®™©]+", "", t)
    t = re.sub(r"[-_.:/\\]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _limpiar_consulta_app(nombre: str) -> str:
    """Limpia artículos o frases de relleno ('la aplicación X', 'X del escritorio') y aplica alias."""
    t = _normalizar_nombre_app(nombre)
    t = re.sub(
        r"^(?:la\s+aplicacion(?:\s+de)?|el\s+programa(?:\s+de)?|la\s+app(?:\s+de)?|app(?:\s+de)?|el|la|un|una)\s+",
        "",
        t,
    )
    t = re.sub(r"\s+(?:de\s+escritorio|del\s+escritorio|en\s+el\s+escritorio|en\s+escritorio)$", "", t)
    t = t.strip()
    return _ALIAS_APPS_ESCRITORIO.get(t, t)


def _carpetas_escritorio() -> list[str]:
    """Devuelve rutas existentes del escritorio del usuario (OneDrive, local y Público)."""
    candidatas = [
        os.path.join(ONEDRIVE_DIR, "Desktop"),
        os.path.join(HOME_DIR, "Desktop"),
        os.path.join(os.getenv("PUBLIC", r"C:\Users\Public"), "Desktop"),
    ]
    rutas: list[str] = []
    vistas: set[str] = set()
    for ruta in candidatas:
        if not ruta or not os.path.isdir(ruta):
            continue
        clave = os.path.normcase(os.path.abspath(ruta))
        if clave not in vistas:
            vistas.add(clave)
            rutas.append(ruta)
    return rutas


def _recolectar_accesos_escritorio() -> list[tuple[str, str, str]]:
    """Lista accesos directos y ejecutables del escritorio como (nombre_visible, tipo, ruta)."""
    resultados: list[tuple[str, str, str]] = []
    for carpeta in _carpetas_escritorio():
        try:
            with os.scandir(carpeta) as entries:
                for entry in entries:
                    if not entry.is_file():
                        continue
                    nombre_lower = entry.name.lower()
                    if not nombre_lower.endswith(_EXTENSIONES_LANZABLES):
                        continue
                    base = os.path.splitext(entry.name)[0]
                    resultados.append((base, "escritorio", entry.path))
        except OSError:
            continue
    return resultados


def _recolectar_accesos_menu_inicio() -> list[tuple[str, str, str]]:
    """Lista accesos directos del Menú Inicio como (nombre_visible, tipo, ruta)."""
    raices = [
        os.path.join(os.getenv("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"),
        os.path.join(os.getenv("PROGRAMDATA", r"C:\ProgramData"), "Microsoft", "Windows", "Start Menu", "Programs"),
    ]
    resultados: list[tuple[str, str, str]] = []
    for raiz in raices:
        if not raiz or not os.path.isdir(raiz):
            continue
        for actual, _, archivos in os.walk(raiz):
            for archivo in archivos:
                if not archivo.lower().endswith((".lnk", ".url", ".appref-ms")):
                    continue
                base = os.path.splitext(archivo)[0]
                resultados.append((base, "menu_inicio", os.path.join(actual, archivo)))
    return resultados


def _obtener_start_apps(forzar_refresco: bool = False) -> list[tuple[str, str]]:
    """Obtiene y cachea aplicaciones instaladas (Store/MSIX y Win32) vía Get-StartApps."""
    global _start_apps_cache, _start_apps_timestamp
    ahora = time.monotonic()
    with _start_apps_lock:
        if _start_apps_timestamp > 0.0 and (not forzar_refresco or (ahora - _start_apps_timestamp) < 30.0):
            return list(_start_apps_cache)

    try:
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        ps_cmd = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Get-StartApps | ConvertTo-Json -Compress"
        res = subprocess.run(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=6,
            creationflags=creationflags,
        )
        if res.returncode == 0 and res.stdout.strip():
            raw = json.loads(res.stdout)
            items = [raw] if isinstance(raw, dict) else list(raw)
            parsed: list[tuple[str, str]] = []
            for item in items:
                nombre = str(item.get("Name") or "").strip()
                app_id = str(item.get("AppID") or "").strip()
                if nombre and app_id:
                    parsed.append((nombre, app_id))
            with _start_apps_lock:
                _start_apps_cache = parsed
                _start_apps_timestamp = time.monotonic()
            return parsed
    except Exception:
        pass

    with _start_apps_lock:
        _start_apps_timestamp = time.monotonic()
        return list(_start_apps_cache)


def precalentar_apps_instaladas():
    """Pre-carga la caché de aplicaciones de Windows (Store/MSIX/StartApps) en segundo plano."""
    _obtener_start_apps(forzar_refresco=False)


def _puntuar_coincidencia_app(consulta_norm: str, candidato_norm: str, ruta_o_id: str = "") -> int:
    """Calcula un puntaje de coincidencia (0 = sin coincidencia)."""
    if not consulta_norm or not candidato_norm:
        return 0

    for excl in _PALABRAS_EXCLUIDAS_APP:
        if (excl in candidato_norm or excl in ruta_o_id.lower()) and excl not in consulta_norm:
            return 0

    if candidato_norm == consulta_norm:
        return 1000
    if candidato_norm.startswith(consulta_norm + " "):
        return 860 - min(40, len(candidato_norm) - len(consulta_norm))
    if candidato_norm.startswith(consulta_norm):
        return 760

    palabras_q = consulta_norm.split()
    palabras_c = candidato_norm.split()
    if palabras_q and all(any(pc == pq or pc.startswith(pq) for pc in palabras_c) for pq in palabras_q):
        extra = max(0, len(palabras_c) - len(palabras_q))
        return max(560, 680 - extra * 20)

    if len(consulta_norm) >= 3 and consulta_norm in candidato_norm:
        return 520

    ratio = difflib.SequenceMatcher(None, consulta_norm, candidato_norm).ratio()
    if ratio >= 0.80:
        return int(350 + ratio * 100)
    return 0


def resolver_aplicacion(nombre: str, permitir_refresco: bool = True) -> tuple[str, str, str] | None:
    """Resuelve una app a (tipo, valor, nombre_visible) buscando en Escritorio, sistema, Menú Inicio y Store/MSIX."""
    consulta_norm = _limpiar_consulta_app(nombre)
    if not consulta_norm:
        return None

    accesos_escritorio = _recolectar_accesos_escritorio()
    # 1. Coincidencia exacta en Escritorio (máxima prioridad)
    for visible, tipo, ruta in accesos_escritorio:
        if _normalizar_nombre_app(visible) == consulta_norm:
            return (tipo, ruta, visible)

    # 2. Comandos integrados del sistema (instantáneos, <1ms)
    cmd_sistema = _APPS_SISTEMA.get(consulta_norm)
    if cmd_sistema:
        return ("sistema", cmd_sistema, nombre.strip())

    # 3. Coincidencia puntuada en Escritorio + Menú Inicio + StartApps cacheados
    candidatos: list[tuple[int, str, str, str]] = []
    for visible, tipo, ruta in accesos_escritorio:
        score = _puntuar_coincidencia_app(consulta_norm, _normalizar_nombre_app(visible), ruta)
        if score > 0:
            bonus = 120 if not ruta.lower().endswith(".exe") else 40
            candidatos.append((score + bonus, tipo, ruta, visible))

    for visible, tipo, ruta in _recolectar_accesos_menu_inicio():
        score = _puntuar_coincidencia_app(consulta_norm, _normalizar_nombre_app(visible), ruta)
        if score > 0:
            candidatos.append((score + 50, tipo, ruta, visible))

    for visible, app_id in _obtener_start_apps(forzar_refresco=False):
        score = _puntuar_coincidencia_app(consulta_norm, _normalizar_nombre_app(visible), app_id)
        if score > 0:
            candidatos.append((score, "startapp", app_id, visible))

    if not candidatos and permitir_refresco:
        for visible, app_id in _obtener_start_apps(forzar_refresco=True):
            score = _puntuar_coincidencia_app(consulta_norm, _normalizar_nombre_app(visible), app_id)
            if score > 0:
                candidatos.append((score, "startapp", app_id, visible))

    if candidatos:
        candidatos.sort(key=lambda item: item[0], reverse=True)
        _, tipo, valor, visible = candidatos[0]
        return (tipo, valor, visible)

    exe = shutil.which(consulta_norm) or shutil.which(f"{consulta_norm}.exe")
    if exe:
        return ("exe", exe, nombre.strip())

    return None


def aplicacion_disponible(nombre: str) -> bool:
    """Indica si `nombre` corresponde a una app instalada o acceso directo del escritorio."""
    return resolver_aplicacion(nombre, permitir_refresco=False) is not None


def listar_aplicaciones_escritorio() -> str:
    """Lista las aplicaciones y accesos directos disponibles en el escritorio de Windows."""
    accesos = _recolectar_accesos_escritorio()
    if not accesos:
        return "No encontré accesos directos ni aplicaciones en el escritorio."
    nombres: list[str] = []
    vistos: set[str] = set()
    for visible, _, ruta in sorted(accesos, key=lambda item: item[0].lower()):
        norm = _normalizar_nombre_app(visible)
        if any(excl in norm for excl in _PALABRAS_EXCLUIDAS_APP):
            continue
        if ruta.lower().endswith(".exe") and "whql" in norm:
            continue
        if norm not in vistos:
            vistos.add(norm)
            nombres.append(visible)
    if not nombres:
        return "No encontré aplicaciones lanzables en el escritorio."
    return "En el escritorio tenés: " + ", ".join(nombres[:25]) + "."


def _buscar_acceso_menu_inicio(nombre_lower: str) -> str | None:
    """Mantiene compatibilidad devolviendo la ruta si existe en el Menú Inicio."""
    res = resolver_aplicacion(nombre_lower, permitir_refresco=False)
    if res and res[0] in ("escritorio", "menu_inicio"):
        return res[1]
    return None


def lanzar_aplicacion(nombre: str) -> str:
    """Abre cualquier aplicación de escritorio, acceso directo (.lnk/.url/.exe), Menú Inicio o app Store/MSIX."""
    objetivo = (nombre or "").strip()
    if not objetivo:
        return "No se indicó qué aplicación abrir."

    resuelto = resolver_aplicacion(objetivo, permitir_refresco=True)
    if not resuelto:
        return f"No se encontró la aplicación '{objetivo}' en el escritorio ni en el sistema."

    tipo, valor, visible = resuelto
    try:
        if tipo in ("escritorio", "menu_inicio"):
            os.startfile(valor)
        elif tipo == "sistema":
            subprocess.Popen(valor, shell=True)
        elif tipo == "startapp":
            if "://" in valor or os.path.exists(valor):
                os.startfile(valor)
            else:
                subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{valor}"])
        else:
            subprocess.Popen([valor])
        return f"Aplicación {visible} iniciada correctamente."
    except Exception as e:
        return f"No se pudo iniciar '{visible}': {e}"


def cerrar_aplicacion(nombre: str) -> str:
    """Cierra ventanas/procesos de una aplicación por nombre (ej. 'chrome', 'spotify', 'notepad')."""
    objetivo = _limpiar_consulta_app(nombre)
    if not objetivo:
        return "No se indicó qué aplicación cerrar."

    protegidos = {"explorer", "svchost", "csrss", "wininit", "lsass", "dwm", "system", "pythonw", "piper"}
    alias_proc = {
        "bloc de notas": "notepad",
        "vscode": "code",
        "visual studio code": "code",
        "calculadora": "calculatorapp",
        "edge": "msedge",
        "cloud": "claude",
        "obs studio": "obs64",
        "oracle virtualbox": "virtualbox",
        "utorrent web": "utweb",
    }
    patron = alias_proc.get(objetivo, objetivo).replace(" ", "")
    cerrados = 0

    for proc in psutil.process_iter(["pid", "name"]):
        try:
            pname = (proc.info.get("name") or "").lower()
            base = pname.removesuffix(".exe")
            if not base or base in protegidos:
                continue
            base_compact = base.replace(" ", "").replace("-", "").replace("_", "")
            if patron in base_compact:
                proc.terminate()
                cerrados += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    if cerrados == 0:
        return f"No encontré ninguna aplicación activa llamada '{nombre}'."
    return f"Se cerraron {cerrados} procesos de {nombre}."


def abrir_sitio_web(destino: str) -> str:
    """Abre un sitio web o alias conocido ('gmail', 'youtube', 'whatsapp', 'drive', 'chatgpt', etc.) en el navegador."""
    texto = (destino or "").strip()
    if not texto:
        return "No se indicó qué sitio web abrir."

    alias_sitios = {
        "gmail": "https://mail.google.com",
        "correo": "https://mail.google.com",
        "mail": "https://mail.google.com",
        "youtube": "https://www.youtube.com",
        "whatsapp": "https://web.whatsapp.com",
        "drive": "https://drive.google.com",
        "google drive": "https://drive.google.com",
        "calendar": "https://calendar.google.com",
        "calendario": "https://calendar.google.com",
        "chatgpt": "https://chatgpt.com",
        "github": "https://github.com",
        "netflix": "https://www.netflix.com",
        "twitch": "https://www.twitch.tv",
        "instagram": "https://www.instagram.com",
        "mercadolibre": "https://www.mercadolibre.com.ar",
        "maps": "https://maps.google.com",
    }
    url = alias_sitios.get(texto.lower())
    if not url:
        if texto.startswith(("http://", "https://")):
            url = texto
        elif "." in texto and " " not in texto:
            url = f"https://{texto}"
        else:
            url = f"https://www.google.com/search?q={quote_plus(texto)}"

    webbrowser.open(url)
    return f"Sitio abierto: {url}."


def buscar_en_google(consulta: str) -> str:
    """Abre una búsqueda en Google en el navegador predeterminado."""
    q = (consulta or "").strip()
    if not q:
        return "No se indicó qué buscar."
    webbrowser.open(f"https://www.google.com/search?q={quote_plus(q)}")
    return f"Búsqueda abierta para '{q}'."


def buscar_archivos(patron: str, ubicacion: str = "usuario") -> str:
    """Busca archivos por nombre dentro de una carpeta ('escritorio', 'descargas', 'documentos', 'jarvis', 'usuario')."""
    consulta = (patron or "").strip().lower()
    if not consulta:
        return "No se indicó qué archivo buscar."

    raiz = _resolver_ruta_base(ubicacion)
    if not os.path.exists(raiz):
        return f"La ruta {raiz} no existe."

    ignorar = {"appdata", "node_modules", ".git", "__pycache__", "$recycle.bin", "windows"}
    encontrados: list[str] = []

    for actual, dirs, archivos in os.walk(raiz):
        dirs[:] = [d for d in dirs if d.lower() not in ignorar and not d.startswith(".")]
        for archivo in archivos:
            if consulta in archivo.lower():
                rel = os.path.relpath(os.path.join(actual, archivo), raiz)
                encontrados.append(rel)
                if len(encontrados) >= 5:
                    break
        if len(encontrados) >= 5:
            break

    if not encontrados:
        return f"No encontré archivos que coincidan con '{patron}' en {ubicacion}."
    return "Encontré: " + ", ".join(encontrados) + "."


_PATRONES_PROHIBIDOS = re.compile(
    r"\b(?:format\s+[a-z]:|diskpart|bcdedit|cipher\s+/w|sdelete|rm\s+-rf\s+/|remove-item\b.*-recurse\b.*\b[a-z]:\\$)",
    re.IGNORECASE,
)


def ejecutar_comando_sistema(comando: str) -> str:
    """Ejecuta un comando de PowerShell en Windows para controlar o consultar el sistema con libertad."""
    cmd = (comando or "").strip()
    if not cmd:
        return "No se indicó ningún comando."
    if _PATRONES_PROHIBIDOS.search(cmd):
        return "Comando bloqueado por seguridad del sistema."

    try:
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        res = subprocess.run(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", cmd],
            capture_output=True,
            text=True,
            timeout=12,
            creationflags=creationflags,
        )
        salida = (res.stdout or "").strip()
        err = (res.stderr or "").strip()
        if res.returncode != 0 and not salida:
            return f"Error al ejecutar comando: {err[:180] or 'fallo desconocido'}"
        if salida:
            limpia = re.sub(r"\s+", " ", salida)
            return f"Resultado: {limpia[:240]}"
        return "Comando ejecutado."
    except subprocess.TimeoutExpired:
        return "Error: el comando superó el tiempo límite."
    except Exception as e:
        return f"Error al ejecutar comando: {e}"


def bloquear_estacion_trabajo() -> str:
    """Bloquea inmediatamente la pantalla de Windows."""
    ctypes.windll.user32.LockWorkStation()
    return "Estación de trabajo bloqueada."


def silenciar_volumen() -> str:
    """Mutea o desmutea el volumen general del sistema."""
    ctypes.windll.user32.keybd_event(0xAD, 0, 0, 0)
    ctypes.windll.user32.keybd_event(0xAD, 0, 2, 0)
    return "Estado de volumen alternado."


# --- HERRAMIENTAS DE ACCESO Y GESTIÓN DE ARCHIVOS Y CARPETAS ---
def listar_archivos(ubicacion: str = "jarvis") -> str:
    """Lista los archivos y carpetas dentro de una ubicación ('escritorio', 'descargas', 'documentos', 'jarvis' o ruta)."""
    try:
        ruta = _resolver_ruta_base(ubicacion)
        if not os.path.exists(ruta):
            return f"La ruta {ruta} no existe."
        elementos = os.listdir(ruta)
        if not elementos:
            return f"La carpeta {os.path.basename(ruta) or ruta} está vacía."
        carpetas = [e for e in elementos if os.path.isdir(os.path.join(ruta, e))]
        archivos = [e for e in elementos if os.path.isfile(os.path.join(ruta, e))]
        resumen = []
        if carpetas:
            resumen.append(f"{len(carpetas)} carpetas ({', '.join(carpetas[:6])})")
        if archivos:
            resumen.append(f"{len(archivos)} archivos ({', '.join(archivos[:8])})")
        return f"En {os.path.basename(ruta) or ruta} hay: " + " y ".join(resumen) + "."
    except Exception as e:
        return f"Error al listar archivos: {e}"


def crear_carpeta(nombre_o_ruta: str, ubicacion_base: str = "escritorio") -> str:
    """Crea una carpeta nueva en la ubicación indicada ('escritorio', 'descargas', 'documentos', 'jarvis' o ruta)."""
    try:
        base = _resolver_ruta_base(ubicacion_base)
        destino = (
            nombre_o_ruta
            if os.path.isabs(nombre_o_ruta)
            else os.path.join(base, nombre_o_ruta.strip("\\/"))
        )
        os.makedirs(destino, exist_ok=True)
        return f"Carpeta '{os.path.basename(destino)}' creada en {os.path.dirname(destino)}."
    except Exception as e:
        return f"No se pudo crear la carpeta: {e}"


def escribir_archivo(ruta_archivo: str, contenido: str, modo: str = "sobrescribir") -> str:
    """Crea o modifica un archivo de texto o código en la carpeta del usuario.
    'modo' puede ser 'sobrescribir' o 'agregar'."""
    try:
        ruta = _resolver_ruta_base(ruta_archivo)
        os.makedirs(os.path.dirname(ruta), exist_ok=True)
        flag = "a" if modo.lower() in ("agregar", "append", "añadir") else "w"
        with open(ruta, flag, encoding="utf-8") as f:
            f.write(contenido + ("\n" if flag == "a" and not contenido.endswith("\n") else ""))
        accion = "actualizado" if flag == "a" else "guardado"
        return f"Archivo '{os.path.basename(ruta)}' {accion} correctamente."
    except Exception as e:
        return f"Error al escribir el archivo: {e}"


def leer_archivo(ruta_archivo: str) -> str:
    """Lee el contenido de un archivo de texto en la carpeta de Jarvis, escritorio o ruta indicada."""
    try:
        ruta = _resolver_ruta_base(ruta_archivo)
        if not os.path.exists(ruta):
            return f"No se encontró el archivo {os.path.basename(ruta)}."
        with open(ruta, "r", encoding="utf-8", errors="replace") as f:
            texto = f.read(1500)
        return f"Contenido de {os.path.basename(ruta)}: {texto[:400]}"
    except Exception as e:
        return f"Error al leer el archivo: {e}"


def mover_o_renombrar_archivo(origen: str, destino: str) -> str:
    """Mueve o renombra un archivo o carpeta."""
    try:
        ruta_origen = _resolver_ruta_base(origen)
        ruta_destino = _resolver_ruta_base(destino)
        if not os.path.exists(ruta_origen):
            return f"No existe el origen '{origen}'."
        shutil.move(ruta_origen, ruta_destino)
        return f"Se movió o renombró '{os.path.basename(ruta_origen)}' a '{ruta_destino}'."
    except Exception as e:
        return f"Error al mover o renombrar: {e}"


def eliminar_archivo(ruta_archivo: str) -> str:
    """Elimina un archivo o carpeta especificada por el usuario."""
    try:
        ruta = _resolver_ruta_base(ruta_archivo)
        if not os.path.exists(ruta):
            return f"No se encontró '{ruta_archivo}'."
        if os.path.isdir(ruta):
            shutil.rmtree(ruta)
            return f"Carpeta '{os.path.basename(ruta)}' eliminada."
        os.remove(ruta)
        return f"Archivo '{os.path.basename(ruta)}' eliminado."
    except Exception as e:
        return f"No se pudo eliminar: {e}"


def abrir_carpeta_explorador(ubicacion: str = "jarvis") -> str:
    """Abre una carpeta en el Explorador de archivos de Windows ('escritorio', 'descargas', 'documentos', 'jarvis' o ruta)."""
    try:
        ruta = _resolver_ruta_base(ubicacion)
        if not os.path.exists(ruta):
            return f"La carpeta {ruta} no existe."
        os.startfile(ruta)
        return f"Carpeta '{os.path.basename(ruta) or ruta}' abierta en el Explorador."
    except Exception as e:
        return f"Error al abrir la carpeta: {e}"


LISTA_HERRAMIENTAS = [
    obtener_estado_hardware,
    lanzar_aplicacion,
    cerrar_aplicacion,
    listar_aplicaciones_escritorio,
    abrir_sitio_web,
    buscar_en_google,
    revisar_gmail,
    bloquear_estacion_trabajo,
    silenciar_volumen,
    ajustar_volumen,
    ajustar_volumen_spotify,
    controlar_reproduccion,
    reproducir_musica,
    obtener_cancion_actual,
    listar_playlists_spotify,
    listar_archivos,
    buscar_archivos,
    crear_carpeta,
    escribir_archivo,
    leer_archivo,
    mover_o_renombrar_archivo,
    eliminar_archivo,
    abrir_carpeta_explorador,
    ejecutar_comando_sistema,
]

MAPA_HERRAMIENTAS = {fn.__name__: fn for fn in LISTA_HERRAMIENTAS}

DECLARACIONES_HERRAMIENTAS = [
    {
        "type": "function",
        "name": "obtener_estado_hardware",
        "description": "Devuelve el uso actual de CPU, memoria RAM y porcentaje de disco.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "lanzar_aplicacion",
        "description": (
            "Abre cualquier aplicación de escritorio, juego, acceso directo del Escritorio (.lnk, .url, .exe), "
            "programa del Menú Inicio o app de Microsoft Store/Windows (ej. 'Claude', 'Antigravity', 'Brave', 'Gemini', "
            "'Discord', 'Spotify', 'Visual Studio Code', 'Steam', 'Epic Games', 'OBS Studio', 'Counter-Strike 2', "
            "'Rocket League', 'UltiMaker Cura', 'VirtualBox', 'calculadora', etc.). "
            "USAR SIEMPRE que el usuario pida abrir una aplicación o programa sin indicar explícitamente una URL o navegador web."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "nombre": {
                    "type": "string",
                    "description": "Nombre de la aplicación, juego o acceso directo de escritorio a abrir.",
                },
            },
            "required": ["nombre"],
        },
    },
    {
        "type": "function",
        "name": "cerrar_aplicacion",
        "description": "Cierra una aplicación abierta en Windows por su nombre (ej. 'claude', 'chrome', 'brave', 'spotify', 'discord', 'notepad').",
        "parameters": {
            "type": "object",
            "properties": {
                "nombre": {
                    "type": "string",
                    "description": "Nombre de la aplicación a cerrar.",
                },
            },
            "required": ["nombre"],
        },
    },
    {
        "type": "function",
        "name": "listar_aplicaciones_escritorio",
        "description": "Lista las aplicaciones, juegos y accesos directos disponibles en el escritorio de Windows.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "abrir_sitio_web",
        "description": "Abre únicamente un sitio web o URL en el navegador ('gmail', 'youtube', 'whatsapp', 'drive', 'calendar', 'chatgpt', 'github', o un dominio/URL explícita). NO usar para aplicaciones de escritorio como Claude, Discord, Brave o juegos.",
        "parameters": {
            "type": "object",
            "properties": {
                "destino": {
                    "type": "string",
                    "description": "Nombre del sitio o URL a abrir.",
                },
            },
            "required": ["destino"],
        },
    },
    {
        "type": "function",
        "name": "buscar_en_google",
        "description": "Busca información en Google abriendo el navegador con la consulta.",
        "parameters": {
            "type": "object",
            "properties": {
                "consulta": {
                    "type": "string",
                    "description": "Texto a buscar en Google.",
                },
            },
            "required": ["consulta"],
        },
    },
    {
        "type": "function",
        "name": "revisar_gmail",
        "description": "Revisa los correos recientes o sin leer en Gmail y devuelve remitentes y asuntos (o abre Gmail en el navegador si falta configurar GMAIL_APP_PASSWORD).",
        "parameters": {
            "type": "object",
            "properties": {
                "cantidad": {
                    "type": "integer",
                    "description": "Cantidad máxima de correos a leer (por defecto 5).",
                },
                "solo_no_leidos": {
                    "type": "boolean",
                    "description": "True para ver solo no leídos (por defecto), False para incluir leídos.",
                },
                "consulta": {
                    "type": "string",
                    "description": "Texto opcional para filtrar correos (ej. nombre de remitente o palabra clave).",
                },
            },
        },
    },
    {
        "type": "function",
        "name": "bloquear_estacion_trabajo",
        "description": "Bloquea inmediatamente la pantalla de Windows.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "silenciar_volumen",
        "description": "Mutea o desmutea el volumen general del sistema.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "ajustar_volumen",
        "description": "Ajusta el volumen maestro general de Windows (solo cuando el usuario pide cambiar el volumen general de la PC o de Windows, NO el de la música).",
        "parameters": {
            "type": "object",
            "properties": {
                "porcentaje": {
                    "type": "integer",
                    "description": "Número entero entre 0 y 100 con el nivel de volumen deseado.",
                },
            },
            "required": ["porcentaje"],
        },
    },
    {
        "type": "function",
        "name": "ajustar_volumen_spotify",
        "description": (
            "Ajusta únicamente el volumen de la música o de Spotify (sin tocar el volumen de Windows ni otras aplicaciones). "
            "Úsala siempre que el usuario pida bajar/subir la música, bajar/subir Spotify o poner la música a cierto porcentaje."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "porcentaje": {
                    "type": "integer",
                    "description": "Porcentaje exacto de 0 a 100 para el volumen de la música (opcional si se usa delta).",
                },
                "delta": {
                    "type": "integer",
                    "description": "Cambio relativo (ej: -25 para bajar la música, +25 para subir la música).",
                },
            },
        },
    },
    {
        "type": "function",
        "name": "controlar_reproduccion",
        "description": (
            "Controla la reproducción global de audio en Windows (Spotify, navegador, etc.). "
            "Valores válidos para 'accion': 'play_pause', 'siguiente', 'anterior', 'detener'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "accion": {
                    "type": "string",
                    "description": "Acción multimedia a ejecutar: 'play_pause', 'siguiente', 'anterior' o 'detener'.",
                },
            },
            "required": ["accion"],
        },
    },
    {
        "type": "function",
        "name": "reproducir_musica",
        "description": (
            "Busca y reproduce música en Spotify: canciones, artistas, álbumes, playlists personales/guardadas, "
            "tus 'Me gusta', lo más escuchado o radios (ej. 'radio de Drake', 'mi radio', 'mi playlist gym'). "
            "IMPORTANTE: Si el nombre del artista, canción o playlist en inglés fue transcrito fonéticamente en español "
            "(ej. 'lusuri de aselia' -> 'Luxury Azealia Banks', 'swiss dream' -> 'Sweet Dreams', 'confidence' -> 'Confident', "
            "'bisarap' -> 'Bizarrap', 'teilor suif' -> 'Taylor Swift'), "
            "corrige la ortografía al nombre real oficial en el parámetro 'busqueda', "
            "sin preposiciones como 'de' o 'por' (salvo en radios o playlists con nombre propio). Si el pedido está cortado, es ambiguo o no especifica qué poner, "
            "no llames a esta herramienta con un fragmento dudoso: pedí aclaración al usuario."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "busqueda": {
                    "type": "string",
                    "description": "Nombre corregido de la canción, artista, playlist (ej: 'mis me gusta', 'Gym') o radio (ej: 'Drake', 'mi radio').",
                },
                "tipo": {
                    "type": "string",
                    "description": "Tipo de contenido: 'track', 'artist', 'playlist', 'album' o 'radio'. Por defecto 'track'.",
                },
            },
            "required": ["busqueda"],
        },
    },
    {
        "type": "function",
        "name": "obtener_cancion_actual",
        "description": "Devuelve el nombre de la canción y el artista que está sonando actualmente en Spotify.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "listar_playlists_spotify",
        "description": "Lista las playlists guardadas y personales de la cuenta de Spotify del usuario.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "listar_archivos",
        "description": "Lista los archivos y carpetas en una ubicación ('escritorio', 'descargas', 'documentos', 'jarvis' o ruta).",
        "parameters": {
            "type": "object",
            "properties": {
                "ubicacion": {
                    "type": "string",
                    "description": "Alias ('escritorio', 'descargas', 'documentos', 'jarvis') o ruta de carpeta.",
                },
            },
        },
    },
    {
        "type": "function",
        "name": "crear_carpeta",
        "description": "Crea una nueva carpeta en el escritorio, descargas, documentos, carpeta de Jarvis o ruta indicada.",
        "parameters": {
            "type": "object",
            "properties": {
                "nombre_o_ruta": {
                    "type": "string",
                    "description": "Nombre de la carpeta a crear.",
                },
                "ubicacion_base": {
                    "type": "string",
                    "description": "Dónde crearla: 'escritorio', 'descargas', 'documentos', 'jarvis' o ruta absoluta.",
                },
            },
            "required": ["nombre_o_ruta"],
        },
    },
    {
        "type": "function",
        "name": "escribir_archivo",
        "description": "Crea o modifica un archivo con el contenido indicado (por ejemplo 'escritorio/notas.txt' o 'jarvis/nota.txt').",
        "parameters": {
            "type": "object",
            "properties": {
                "ruta_archivo": {
                    "type": "string",
                    "description": "Nombre o ruta del archivo (ej: 'escritorio/ideas.txt' o 'notas.txt').",
                },
                "contenido": {
                    "type": "string",
                    "description": "Texto completo a escribir en el archivo.",
                },
                "modo": {
                    "type": "string",
                    "description": "'sobrescribir' (por defecto) o 'agregar' para añadir al final.",
                },
            },
            "required": ["ruta_archivo", "contenido"],
        },
    },
    {
        "type": "function",
        "name": "leer_archivo",
        "description": "Lee el contenido de un archivo de texto.",
        "parameters": {
            "type": "object",
            "properties": {
                "ruta_archivo": {
                    "type": "string",
                    "description": "Ruta o nombre del archivo a leer.",
                },
            },
            "required": ["ruta_archivo"],
        },
    },
    {
        "type": "function",
        "name": "mover_o_renombrar_archivo",
        "description": "Mueve o renombra un archivo o carpeta de origen a destino.",
        "parameters": {
            "type": "object",
            "properties": {
                "origen": {"type": "string", "description": "Ruta o nombre actual del archivo/carpeta."},
                "destino": {"type": "string", "description": "Nueva ruta o nuevo nombre del archivo/carpeta."},
            },
            "required": ["origen", "destino"],
        },
    },
    {
        "type": "function",
        "name": "eliminar_archivo",
        "description": "Elimina un archivo o carpeta indicada.",
        "parameters": {
            "type": "object",
            "properties": {
                "ruta_archivo": {"type": "string", "description": "Ruta o nombre del archivo/carpeta a eliminar."},
            },
            "required": ["ruta_archivo"],
        },
    },
    {
        "type": "function",
        "name": "abrir_carpeta_explorador",
        "description": "Abre una carpeta en el Explorador de Windows ('escritorio', 'descargas', 'documentos', 'jarvis' o ruta).",
        "parameters": {
            "type": "object",
            "properties": {
                "ubicacion": {
                    "type": "string",
                    "description": "Carpeta a abrir ('escritorio', 'descargas', 'documentos', 'jarvis' o ruta).",
                },
            },
        },
    },
    {
        "type": "function",
        "name": "buscar_archivos",
        "description": "Busca archivos por nombre dentro de una carpeta ('escritorio', 'descargas', 'documentos', 'jarvis', 'usuario').",
        "parameters": {
            "type": "object",
            "properties": {
                "patron": {"type": "string", "description": "Parte del nombre del archivo a buscar."},
                "ubicacion": {"type": "string", "description": "Carpeta base donde buscar (por defecto 'usuario')."},
            },
            "required": ["patron"],
        },
    },
    {
        "type": "function",
        "name": "ejecutar_comando_sistema",
        "description": "Ejecuta una orden directa de PowerShell/Windows para tareas o consultas del sistema que no cubren otras herramientas (ej. vaciar papelera, consultar IP, procesos activos, ajustes).",
        "parameters": {
            "type": "object",
            "properties": {
                "comando": {
                    "type": "string",
                    "description": "Comando de PowerShell conciso a ejecutar.",
                },
            },
            "required": ["comando"],
        },
    },
]
