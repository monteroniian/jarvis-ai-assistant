"""Herramienta para revisar correos de Gmail vía IMAP (solo lectura) o abrir Gmail en el navegador."""

import email
from email.header import decode_header
import imaplib
import os
import re
import webbrowser


def _decodificar_cabecera(valor: str | None) -> str:
    if not valor:
        return ""
    partes = []
    for fragmento, charset in decode_header(valor):
        if isinstance(fragmento, bytes):
            try:
                partes.append(fragmento.decode(charset or "utf-8", errors="replace"))
            except Exception:
                partes.append(fragmento.decode("latin-1", errors="replace"))
        else:
            partes.append(str(fragmento))
    return re.sub(r"\s+", " ", "".join(partes)).strip()


def _limpiar_remitente(remitente: str) -> str:
    texto = _decodificar_cabecera(remitente)
    nombre = re.sub(r"\s*<[^>]+>", "", texto).strip().strip('"').strip("'")
    return nombre or texto


def revisar_gmail(cantidad: int = 5, solo_no_leidos: bool = True, consulta: str = "") -> str:
    """Revisa correos recientes o sin leer en Gmail por IMAP, o abre Gmail en el navegador si no hay credenciales."""
    usuario = (os.getenv("GMAIL_USER") or "").strip()
    clave_app = re.sub(r"\s+", "", os.getenv("GMAIL_APP_PASSWORD") or "")

    if not usuario or not clave_app:
        webbrowser.open("https://mail.google.com")
        return (
            "No encontré GMAIL_USER o GMAIL_APP_PASSWORD en el archivo .env, "
            "así que abrí Gmail en el navegador."
        )

    limite = max(1, min(10, int(cantidad)))
    criterios = ["UNSEEN" if solo_no_leidos else "ALL"]
    consulta_limpia = (consulta or "").strip().replace('"', "")
    if consulta_limpia:
        criterios.append(f'TEXT "{consulta_limpia}"')

    imap = None
    try:
        imap = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=8)
        imap.login(usuario, clave_app)
        imap.select("INBOX", readonly=True)

        estado, datos = imap.search(None, *criterios)
        if estado != "OK":
            return "No se pudo consultar la bandeja de Gmail."

        ids = datos[0].split() if datos and datos[0] else []
        if not ids:
            return "No tenés correos sin leer en Gmail." if solo_no_leidos else "No encontré correos con ese criterio."

        total = len(ids)
        seleccionados = list(reversed(ids[-limite:]))
        resumenes: list[str] = []

        for uid in seleccionados:
            st, msg_data = imap.fetch(uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
            if st != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            de = _limpiar_remitente(msg.get("From", "Desconocido"))
            asunto = _decodificar_cabecera(msg.get("Subject")) or "(Sin asunto)"
            resumenes.append(f"De {de}: {asunto}")

        if not resumenes:
            return f"Tenés {total} correos, pero no pude leer sus encabezados."

        encabezado = f"Tenés {total} correos sin leer." if solo_no_leidos else f"Encontré {total} correos."
        return f"{encabezado} Los más recientes son: " + "; ".join(resumenes) + "."
    except imaplib.IMAP4.error:
        return "No se pudo iniciar sesión en Gmail. Verificá GMAIL_USER y la contraseña de aplicación GMAIL_APP_PASSWORD en .env."
    except Exception as e:
        return f"Error al revisar Gmail: {e}"
    finally:
        if imap is not None:
            try:
                imap.logout()
            except Exception:
                pass
