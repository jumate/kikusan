"""Last.fm similar artists plugin."""

import logging
import time
from collections import defaultdict
from typing import List, Dict, Any

import random 
import os
import httpx
import hashlib
import secrets

import requests
from ytmusicapi import YTMusic

from kikusan.plugins.base import Plugin, Song
from kikusan.models.search import Track

logger = logging.getLogger(__name__)

LASTFM_API_URL = "http://ws.audioscrobbler.com/2.0/"

class LastfmSimilarPlugin(Plugin):
    """Plugin to fetch top songs from similar artists based on Last.fm."""

    @property
    def name(self) -> str:
        return "lastfm_similar"

    @property
    def config_schema(self) -> Dict[str, Any]:
        return {
            "required": ["api_key", "username"],
            "optional": {
                "similar_artists_limit": "Número de artistas similares a extraer (por defecto 5)",
                "songs_per_artist": "Número de canciones top por artista (por defecto 5)",
                "flow_name": "Nombre de la carpeta, álbum y playlist (ej. 'Flow Last.fm')",
                "exclude_artists": "Lista de artistas separados por comas que NO quieres descargar (ej. 'Nena Daconte, Melendi')"
            }
        }

    def _is_already_in_navidrome(self, artist: str, title: str) -> bool:
        """Comprueba si una canción ya existe en Navidrome usando la API de Subsonic."""
        nav_url = os.getenv("NAVIDROME_URL")
        nav_user = os.getenv("NAVIDROME_USER")
        nav_pass = os.getenv("NAVIDROME_PASSWORD")

        if not all([nav_url, nav_user, nav_pass]):
            # Si no hay credenciales, no podemos comprobar
            return False

        try:
            # Generar token de autenticación (salt + hash)
            salt = secrets.token_hex(8)
            token = hashlib.md5((nav_pass + salt).encode('utf-8')).hexdigest()

            query = f"{artist} {title}"
            params = {
                "u": nav_user,
                "t": token,
                "s": salt,
                "v": "1.16.1",
                "c": "Kikusan",
                "f": "json",
                "query": query,
                "songCount": 1
            }

            # Hacemos la petición a Navidrome
            response = httpx.get(f"{nav_url}/rest/search3.view", params=params, timeout=10)
            response.raise_for_status()
            data = response.json()

            # Si hay al menos una canción en el resultado, ya la tenemos
            songs = data.get("subsonic-response", {}).get("searchResult3", {}).get("song", [])
            if songs:
                logger.info("  Ya existe en Navidrome: %s - %s", artist, title)
                return True
            else:
                logger.debug("  No existe en Navidrome: %s - %s", artist, title)                 

        except Exception as e:
            logger.warning("  Error al comprobar en Navidrome: %s", e)

        return False
        
    def fetch_songs(self, config: dict) -> List[Song]:
        """Fetch top songs from similar artists (Kikusan interface)."""
        api_key = config["api_key"]
        username = config["username"]
        num_artists = int(config.get("similar_artists_limit", 5))
        songs_per_artist = int(config.get("songs_per_artist", 5))

        # 1. Obtener recomendaciones de Last.fm
        recommended_artists = self._get_lastfm_recommendations(api_key, username, num_artists)

        # --- NUEVO: Aplicar lista de exclusión (lista negra) ---
        exclude_str = config.get("exclude_artists", "")
        if exclude_str:
            # Convertimos el texto "Nena Daconte, Melendi" en un set ["nena daconte", "melendi"]
            exclude_set = {a.strip().lower() for a in exclude_str.split(",") if a.strip()}
            original_count = len(recommended_artists)
            
            # Filtramos la lista
            recommended_artists = [a for a in recommended_artists if a.lower() not in exclude_set]
            
            if len(recommended_artists) < original_count:
                logger.info("Se han excluido %d artistas por la lista negra.", original_count - len(recommended_artists))
        # -------------------------------------------------------

        logger.info("Artistas recomendados por Last.fm: %s", recommended_artists)

        # 2. Buscar sus top canciones en YouTube Music
        yt = YTMusic()
        songs = []

        for artist_name in recommended_artists:
            logger.info("Buscando top canciones de '%s' en YouTube Music...", artist_name)
            try:
                search_results = yt.search(artist_name, filter="artists", limit=1)
                if not search_results:
                    logger.warning("No se encontró el artista '%s' en YT Music", artist_name)
                    continue
                
                artist_id = search_results[0]["browseId"]
                artist_data = yt.get_artist(artist_id)
                
                top_songs = artist_data.get("songs", {}).get("results", [])
                
                for yt_song in top_songs[:songs_per_artist]:
                    # Creamos el objeto Song que Kikusan espera
                    songs.append(
                        Song(
                            artist=artist_name,
                            title=yt_song["title"],
                        )
                    )
            except Exception as e:
                logger.error("Error buscando en YT Music para '%s': %s", artist_name, e)
            
            time.sleep(1) # Evitar bloqueos

        logger.info("Total de canciones encontradas para descargar: %d", len(songs))
        return songs

    def _get_lastfm_recommendations(self, api_key: str, username: str, limit: int) -> List[str]:
        """Lógica de recomendación con aleatoriedad para evitar repeticiones."""
        
        def _request(method: str, params: dict) -> dict:
            params.update({
                "method": method, "api_key": api_key, "user": username, "format": "json"
            })
            try:
                response = requests.get(LASTFM_API_URL, params=params)
                response.raise_for_status()
                data = response.json()
                if "error" in data:
                    logger.error("Error de Last.fm: %s", data)
                return data
            except requests.exceptions.RequestException as e:
                logger.error("Error en Last.fm API (%s): %s", method, e)
                return {}

        def get_similar_artists(artist_name: str) -> List[Dict]:
            params = {
                "method": "artist.getSimilar",
                "artist": artist_name, 
                "limit": 30, # <-- Aumentamos a 30 para tener más donde elegir
                "api_key": api_key, 
                "format": "json"
            }
            try:
                response = requests.get(LASTFM_API_URL, params=params)
                response.raise_for_status()
                data = response.json()
                if "error" in data:
                    logger.error("Error buscando similares a %s: %s", artist_name, data)
                
                # MEJORA: Mezclamos la lista de similares antes de devolverla
                similars = data.get("similarartists", {}).get("artist", [])
                random.shuffle(similars)
                return similars
            except Exception as e:
                logger.error("Error en get_similar_artists: %s", e)
                return []

        # 1. Obtener semillas (Top artistas del último mes)
        # Pedimos hasta 100 artistas para tener un "pool" grande
        data = _request("user.getTopArtists", {"period": "1month", "limit": 100})
        artists = data.get("topartists", {}).get("artist", [])
        
        # MEJORA: Mezclamos todos tus artistas top y elegimos solo 15 al azar
        random.shuffle(artists)
        selected_artists = artists[:15]

        seeds = []
        for i, artist in enumerate(selected_artists):
            weight = 1.0 - (i * 0.02)
            seeds.append({"name": artist["name"], "weight": max(0.5, weight)})
        
        if not seeds: 
            return []

        # Historial de 6 meses para filtrar conocidos
        logger.info("Obteniendo historial de 6 meses de Last.fm para filtrar conocidos...")
        recent_data = _request("user.getTopArtists", {"period": "6month", "limit": 200})
        recent_artists = recent_data.get("topartists", {}).get("artist", [])
        
        known_artists = {a["name"].lower() for a in recent_artists}
        known_artists.update({s["name"].lower() for s in seeds})
        logger.info("Filtrando contra %d artistas que has escuchado en los últimos 6 meses", len(known_artists))

        candidates = defaultdict(lambda: {"score": 0.0, "seeds": set(), "match_sum": 0.0, "count": 0})
        
        for seed in seeds:
            time.sleep(0.25)
            similar_artists = get_similar_artists(seed["name"])
            
            # MEJORA: Como la lista ya viene mezclada, cogemos los primeros 10
            # (que serán 10 aleatorios de los 30 mejores)
            for sim in similar_artists[:10]:
                name = sim["name"]
                
                if name.lower() in known_artists: 
                    continue
                
                match_val = float(sim.get("match", 0)) / 100.0
                contribution = match_val * seed["weight"]
                
                cand_data = candidates[name]
                cand_data["score"] += contribution
                cand_data["seeds"].add(seed["name"])
                cand_data["match_sum"] += match_val
                cand_data["count"] += 1

        final_list = []
        for name, data in candidates.items():
            coverage_bonus = len(data["seeds"]) * 0.5
            final_score = data["score"] + coverage_bonus
            final_list.append({"artist": name, "score": round(final_score, 3)})

        # Ordenamos por puntuación y nos quedamos con los "limit" mejores
        final_list.sort(key=lambda x: x["score"], reverse=True)
        return [item["artist"] for item in final_list[:limit]]