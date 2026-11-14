"""Last.fm similar artists plugin."""

import logging
import time
from collections import defaultdict
from typing import List, Dict, Any

import os
import httpx
import hashlib
import secrets
import random
import json

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
                "exclude_artists": "Lista de artistas separados por comas que NO quieres descargar (ej. 'Nena Daconte')",
                "purge_rating": "Si se establece a 1, borrará las canciones con 1 estrella en Navidrome."
            }
        }

    def _get_navidrome_auth(self) -> dict | None:
        """Genera los parámetros de autenticación para la API de Subsonic."""
        nav_url = os.getenv("NAVIDROME_URL")
        nav_user = os.getenv("NAVIDROME_USER")
        nav_pass = os.getenv("NAVIDROME_PASSWORD")

        if not all([nav_url, nav_user, nav_pass]):
            return None

        salt = secrets.token_hex(8)
        token = hashlib.md5((nav_pass + salt).encode('utf-8')).hexdigest()
        
        return {
            "url": nav_url,
            "params": {
                "u": nav_user,
                "t": token,
                "s": salt,
                "v": "1.16.1",
                "c": "Kikusan",
                "f": "json",
            }
        }

    def _is_already_in_navidrome(self, artist: str, title: str) -> bool:
        """Comprueba si una canción ya existe en Navidrome usando la API de Subsonic."""
        auth = self._get_navidrome_auth()
        if not auth: return False

        try:
            params = auth["params"].copy()
            params.update({
                "query": f"{artist} {title}",
                "songCount": 1
            })

            response = httpx.get(f"{auth['url']}/rest/search3.view", params=params, timeout=10)
            response.raise_for_status()
            data = response.json()

            songs = data.get("subsonic-response", {}).get("searchResult3", {}).get("song", [])
            if songs:
                logger.info("  Ya existe en Navidrome: %s - %s", artist, title)
                return True
            else:
                logger.debug("  No existe en Navidrome: %s - %s", artist, title)                 

        except Exception as e:
            logger.warning("  Error al comprobar en Navidrome: %s", e)

        return False

    def get_disliked_songs(self, rating_threshold: int = 1) -> set:
        """Obtiene una lista de todas las canciones con la calificación indicada (lógica optimizada)."""
        nav_url = os.getenv("NAVIDROME_URL")
        nav_user = os.getenv("NAVIDROME_USER")
        nav_pass = os.getenv("NAVIDROME_PASSWORD")

        if not all([nav_url, nav_user, nav_pass]):
            return set()

        nav_url = nav_url.rstrip('/')

        try:
            # 1. Autenticación en la API nativa
            res = httpx.post(f'{nav_url}/auth/login', json={'username': nav_user, 'password': nav_pass}, timeout=10)
            res.raise_for_status()
            headers = {'X-ND-Authorization': f'Bearer {res.json()["token"]}'}

            rated, start, page = [], 0, 500
            while True:
                params = {
                    '_start': start, 
                    '_end': start + page, 
                    '_sort': 'title', 
                    '_order': 'ASC',
                    '_filters': json.dumps({'has_rating': True})
                }
                r = httpx.get(f'{nav_url}/api/song', params=params, headers=headers, timeout=15)
                r.raise_for_status()
                batch = r.json()
                rated += batch
                if len(batch) < page:
                    break
                start += page

            disliked = set()
            for c in rated:
                if c.get('rating') == rating_threshold:
                    title = c.get('title', '').lower().strip()
                    artist = c.get('artist', '').lower().strip()
                    disliked.add((title, artist))
                    
            return disliked

        except Exception as e:
            logger.warning("  Error al obtener la lista de canciones con %d estrellas de Navidrome: %s", rating_threshold, e)
            return set()

    def trigger_navidrome_scan(self):
        """Lanza un escaneo completo en Navidrome para actualizar la base de datos tras borrar archivos."""
        auth = self._get_navidrome_auth()
        if not auth:
            return

        try:
            logger.info("Lanzando escaneo completo en Navidrome...")
            params = auth["params"].copy()
            params["fullScan"] = "true"
            response = httpx.get(f"{auth['url']}/rest/startScan", params=params, timeout=10)
            response.raise_for_status()
            logger.info("Escaneo de Navidrome iniciado correctamente.")
        except Exception as e:
            logger.warning("No se pudo iniciar el escaneo en Navidrome: %s", e)

    def fetch_songs(self, config: dict) -> List[Song]:
        """Fetch top songs from similar artists (Kikusan interface)."""
        api_key = config["api_key"]
        username = config["username"]
        num_artists = int(config.get("similar_artists_limit", 5))
        songs_per_artist = int(config.get("songs_per_artist", 5))

        if num_artists == 0 or songs_per_artist == 0:
            logger.info("Límite de artistas o canciones configurado a 0. Saltando búsqueda de nuevas canciones.")
            return []

        # 1. Obtener recomendaciones de Last.fm
        recommended_artists = self._get_lastfm_recommendations(api_key, username, num_artists)

        # Aplicar lista de exclusión (lista negra)
        exclude_str = config.get("exclude_artists", "")
        if exclude_str:
            exclude_set = {a.strip().lower() for a in exclude_str.split(",") if a.strip()}
            original_count = len(recommended_artists)
            recommended_artists = [a for a in recommended_artists if a.lower() not in exclude_set]
            if len(recommended_artists) < original_count:
                logger.info("Se han excluido %d artistas por la lista negra.", original_count - len(recommended_artists))

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
                    songs.append(Song(artist=artist_name, title=yt_song["title"]))
            except Exception as e:
                logger.error("Error buscando en YT Music para '%s': %s", artist_name, e)
            
            time.sleep(1) # Evitar bloqueos

        logger.info("Total de canciones encontradas para descargar: %d", len(songs))
        return songs

    def _get_lastfm_recommendations(self, api_key: str, username: str, limit: int) -> List[str]:
        """Lógica de recomendación con aleatoriedad para evitar repeticiones."""
        
        def _request(method: str, params: dict) -> dict:
            params.update({"method": method, "api_key": api_key, "user": username, "format": "json"})
            try:
                response = requests.get(LASTFM_API_URL, params=params)
                response.raise_for_status()
                data = response.json()
                if "error" in data: logger.error("Error de Last.fm: %s", data)
                return data
            except requests.exceptions.RequestException as e:
                logger.error("Error en Last.fm API (%s): %s", method, e)
                return {}

        def get_similar_artists(artist_name: str) -> List[Dict]:
            params = {"method": "artist.getSimilar", "artist": artist_name, "limit": 30, "api_key": api_key, "format": "json"}
            try:
                response = requests.get(LASTFM_API_URL, params=params)
                response.raise_for_status()
                data = response.json()
                if "error" in data: logger.error("Error buscando similares a %s: %s", artist_name, data)
                similars = data.get("similarartists", {}).get("artist", [])
                random.shuffle(similars)
                return similars
            except Exception as e:
                logger.error("Error en get_similar_artists: %s", e)
                return []

        data = _request("user.getTopArtists", {"period": "1month", "limit": 100})
        artists = data.get("topartists", {}).get("artist", [])
        random.shuffle(artists)
        selected_artists = artists[:15]

        seeds = [{"name": a["name"], "weight": max(0.5, 1.0 - (i * 0.02))} for i, a in enumerate(selected_artists)]
        
        if not seeds: return []

        recent_data = _request("user.getTopArtists", {"period": "6month", "limit": 200})
        recent_artists = recent_data.get("topartists", {}).get("artist", [])
        
        known_artists = {a["name"].lower() for a in recent_artists}
        known_artists.update({s["name"].lower() for s in seeds})
        logger.info("Filtrando contra %d artistas que has escuchado en los últimos 6 meses", len(known_artists))

        candidates = defaultdict(lambda: {"score": 0.0, "seeds": set(), "match_sum": 0.0, "count": 0})
        
        for seed in seeds:
            time.sleep(0.25)
            similar_artists = get_similar_artists(seed["name"])
            
            for sim in similar_artists[:10]:
                name = sim["name"]
                if name.lower() in known_artists: continue
                
                match_val = float(sim.get("match", 0)) / 100.0
                cand_data = candidates[name]
                cand_data["score"] += match_val * seed["weight"]
                cand_data["seeds"].add(seed["name"])
                cand_data["match_sum"] += match_val
                cand_data["count"] += 1

        final_list = []
        for name, data in candidates.items():
            final_score = data["score"] + (len(data["seeds"]) * 0.5)
            final_list.append({"artist": name, "score": round(final_score, 3)})

        final_list.sort(key=lambda x: x["score"], reverse=True)
        return [item["artist"] for item in final_list[:limit]]