# J.A.R.V.I.S. AI Assistant

Asistente personal de voz para Windows con **Gemini API**, síntesis de voz local (**Piper TTS + RVC ONNX**), interfaz **HUD web** y herramientas modulares para controlar Windows, Spotify, Gmail y multimedia.

## Características
- Control de hardware, volumen, aplicaciones y bloqueo de Windows (	ools_windows.py)
- Integración con Spotify API (	ools_spotify.py) y Gmail (	ools_gmail.py)
- Interfaz HUD web en tiempo real (jarvis_hud_interface.html + server.py)
- Síntesis de voz con Piper TTS y conversión RVC ONNX (	ts_piper.py, vc_onnx.py)

## Configuración
1. Crear un archivo .env con tus claves de API (Gemini, Spotify, Gmail).
2. Instalar dependencias de Python y ejecutar python server.py o python jarvis_core.py.
