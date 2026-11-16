import logging
import json
from lastfm_similar import LastfmSimilarPlugin

# Configurar para ver todos los mensajes en consola
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

# --- CONFIGURACIÓN DE PRUEBA ---
# (Pon aquí las mismas claves que en tu config.yml)
test_config = {
    "api_key": "127fd69e5cb59d4550fd9db0eeb55ead",
    "username": "jumate",
    "discover_limit": 3,
    "library_limit": 2,
    "new_releases_limit": 1,
    "tracks_per_artist": 1,
    "seed_artists": "Dire Straits, Pink Floyd",
    "use_recent_listening": True,
    "deeper_cuts": False,
    "only_tags": "",
    "skip_tags": "",
    "exclude_artists": ""
}

# --- IMPORTANTE: Variables de entorno para Navidrome ---
# El script necesita saber quién eres en Navidrome para separar Discover/Library
import os
os.environ["NAVIDROME_URL"] = "https://musica.mireino.duckdns.org"
os.environ["NAVIDROME_USER"] = "juanma"
os.environ["NAVIDROME_PASSWORD"] = "@Vr52n78"

def run_test():
    print("\n--- INICIANDO PRUEBA DEL PLUGIN ---")
    plugin = LastfmSimilarPlugin()
    
    try:
        canciones = plugin.fetch_songs(test_config)
        
        print("\n--- RESULTADOS DE LA PRUEBA ---")
        print(f"Se han encontrado {len(canciones)} canciones.")
        for i, c in enumerate(canciones, 1):
            print(f"{i}. [{c.reason}] {c.artist} - {c.title}")
            
    except Exception as e:
        print(f"Error durante la prueba: {e}")

if __name__ == "__main__":
    run_test()