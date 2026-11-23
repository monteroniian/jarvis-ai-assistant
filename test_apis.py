# test_apis.py
import os
from dotenv import load_dotenv
from google import genai

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# Test Gemini (usando el SDK actual google-genai y gemini-3.8-flash)
try:
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    res = client.interactions.create(
        model="gemini-3.8-flash",
        input="Di 'Sistemas en línea' en una frase.",
    )
    print(f"[OK] Gemini: {res.output_text.strip()}")
except Exception as e:
    print(f"[ERROR] Gemini: {e}")

# Test Spotify
try:
    from tools_spotify import sp, _obtener_playlists_usuario
    usuario = sp.current_user()
    playlists = _obtener_playlists_usuario(max_items=20)
    print(f"[OK] Spotify conectado como: {usuario['display_name']} ({len(playlists)} playlists accesibles)")
except Exception as e:
    print(f"[ERROR] Spotify: {e}")
