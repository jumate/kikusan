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
                "flow_name": "Nombre de la carpeta, álbum y playlist (ej. 'Flow Last.fm')",
                "discover_limit": "Cantidad de canciones de artistas nuevos (Discover) (por defecto 3)",
                "library_limit": "Cantidad de canciones de artistas que ya tienes (Library) (por defecto 3)",
                "new_releases_limit": "Cantidad de novedades (New releases) (por defecto 1)",
                "tracks_per_artist": "Canciones a extraer por artista en cada categoría (por defecto 1)",
                "seed_artists": "Artistas semilla separados por comas (Similar to: ej. 'Marea, Extremoduro')",
                "use_recent_listening": "Usar escuchas recientes (Top 100 + filtro 6 meses) (por defecto true)",
                "deeper_cuts": "Preferir canciones no tan populares (por defecto false)",
                "only_tags": "Incluir SOLO artistas con estas etiquetas (separadas por comas)",
                "skip_tags": "Omitir artistas con estas etiquetas (separadas por comas)",
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

    def _get_all_navidrome_artists(self) -> set:
        """Obtiene todos los nombres de artistas de la librería de Navidrome en minúsculas."""
        auth = self._get_navidrome_auth()
        if not auth: 
            logger.warning("Navidrome no está configurado. Todo se tratará como 'Discover'.")
            return set()

        try:
            params = auth["params"].copy()
            response = httpx.get(f"{auth['url']}/rest/getArtists.view", params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
            
            indices = data.get("subsonic-response", {}).get("artists", {}).get("index", [])
            artists = set()
            for idx in indices:
                for artist in idx.get("artist", []):
                    artists.add(artist["name"].lower())
            logger.info("Encontrados %d artistas en Navidrome.", len(artists))
            return artists
        except Exception as e:
            logger.warning("Error al obtener artistas de Navidrome: %s", e)
            return set()

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

    def get_disliked_songs(self, rating_threshold: int = 1) -> list[dict]:
        """Obtiene una lista de todas las canciones con la calificación indicada (lógica optimizada).

        Devuelve una lista de diccionarios: [{title, artist, path}].
        """
        nav_url = os.getenv("NAVIDROME_URL")
        nav_user = os.getenv("NAVIDROME_USER")
        nav_pass = os.getenv("NAVIDROME_PASSWORD")

        if not all([nav_url, nav_user, nav_pass]):
            return []

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

            return [
                {
                    "title": c.get("title", ""),
                    "artist": c.get("artist", ""),
                    "path": c.get("path", ""),
                }
                for c in rated
                if c.get("rating") == rating_threshold
            ]

        except Exception as e:
            logger.warning("  Error al obtener la lista de canciones con %d estrellas de Navidrome: %s", rating_threshold, e)
            return []

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

    # =========================================================================
    # LÓGICA DE LAST.FM
    # =========================================================================

    def _request_lastfm(self, api_key: str, method: str, params: dict) -> dict:
        params.update({"method": method, "api_key": api_key, "format": "json"})
        try:
            response = requests.get(LASTFM_API_URL, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            if "error" in data: logger.error("Error de Last.fm: %s", data)
            return data
        except Exception as e:
            logger.error("Error en Last.fm API (%s): %s", method, e)
            return {}

    def _get_artist_tags(self, api_key: str, artist_name: str) -> set:
        data = self._request_lastfm(api_key, "artist.getTopTags", {"artist": artist_name})
        tags = data.get("toptags", {}).get("tag", [])
        return {t["name"].lower() for t in tags}

    def _passes_tag_filters(self, api_key: str, artist_name: str, only_tags: set, skip_tags: set) -> bool:
        """Comprueba si un artista pasa los filtros de etiquetas."""
        if not only_tags and not skip_tags:
            return True
        artist_tags = self._get_artist_tags(api_key, artist_name)
        time.sleep(0.2) # Evitar rate limit
        if only_tags and not artist_tags.intersection(only_tags):
            return False
        if skip_tags and artist_tags.intersection(skip_tags):
            return False
        return True

    def _get_scored_candidates(self, api_key: str, username: str, use_recent: bool, manual_seeds: List[str]) -> List[Dict]:
        """Lógica de recomendación con aleatoriedad para evitar repeticiones."""
        
        seeds = [{"name": s, "weight": 1.0} for s in manual_seeds]
        known_artists = set()
        
        if use_recent:
            logger.info("Obteniendo Top 100 artistas del último mes para semillas...")
            data = self._request_lastfm(api_key, "user.getTopArtists", {"user": username, "period": "1month", "limit": 100})
            artists = data.get("topartists", {}).get("artist", [])
            random.shuffle(artists)
            selected_artists = artists[:15]
            
            for i, a in enumerate(selected_artists):
                seeds.append({"name": a["name"], "weight": max(0.5, 1.0 - (i * 0.02))})

            logger.info("Filtrando contra artistas escuchados en los últimos 3 meses...")
            recent_data = self._request_lastfm(api_key, "user.getTopArtists", {"user": username, "period": "3month", "limit": 200})
            recent_artists = recent_data.get("topartists", {}).get("artist", [])
            known_artists = {a["name"].lower() for a in recent_artists}
        
        known_artists.update({s["name"].lower() for s in seeds})

        if not seeds: return []

        candidates = defaultdict(lambda: {"score": 0.0, "seeds": set(), "match_sum": 0.0, "count": 0})
        
        logger.info("Buscando artistas similares a %d semillas...", len(seeds))
        for seed in seeds:
            data = self._request_lastfm(api_key, "artist.getSimilar", {"artist": seed["name"], "limit": 30})
            similars = data.get("similarartists", {}).get("artist", [])
            random.shuffle(similars)
            
            for sim in similars[:10]:
                name = sim["name"]
                if name.lower() in known_artists: continue
                
                match_val = float(sim.get("match", 0)) / 100.0
                cand_data = candidates[name]
                cand_data["score"] += match_val * seed["weight"]
                cand_data["seeds"].add(seed["name"])
                cand_data["match_sum"] += match_val
                cand_data["count"] += 1
            time.sleep(0.25)

        final_list = []
        for name, data in candidates.items():
            final_score = data["score"] + (len(data["seeds"]) * 0.5)
            final_list.append({"artist": name, "score": round(final_score, 3)})

        final_list.sort(key=lambda x: x["score"], reverse=True)
        return final_list

    # =========================================================================
    # YOUTUBE MUSIC Y MIXING
    # =========================================================================

    def _get_new_releases(self, api_key: str, username: str, limit: int) -> List[Dict]:
        data = self._request_lastfm(api_key, "user.getNewReleases", {"user": username, "limit": limit * 2, "userec": 1})
        albums = data.get("albums", {}).get("album", [])
        return [{"artist": a["artist"]["name"], "album": a["name"]} for a in albums]

    
    def _get_yt_tracks(self, yt: YTMusic, artist_name: str, limit: int, reason: str, deeper: bool = False, album_name: str = None) -> List[Song]:
        try:
            if album_name:
                search_results = yt.search(f"{artist_name} {album_name}", filter="albums", limit=1)
                if not search_results: return []
                album_data = yt.get_album(search_results[0]["browseId"])
                tracks = album_data.get("tracks", [])
                if tracks:
                    t = random.choice(tracks)
                    return [Song(artist=artist_name, title=t["title"], reason=reason)]
                return []

            search_results = yt.search(artist_name, filter="artists", limit=1)
            if not search_results: return []
            artist_id = search_results[0]["browseId"]
            
            # --- INICIO DEL FIX (Fallback para Antonio Orozco y similares) ---
            try:
                artist_data = yt.get_artist(artist_id)
                top_songs = artist_data.get("songs", {}).get("results", [])
            except Exception as e:
                # Si get_artist falla (error de estructura de YouTube), usamos search como plan B
                logger.debug("get_artist falló para %s, usando fallback de búsqueda: %s", artist_name, e)
                songs_results = yt.search(artist_name, filter="songs", limit=30)
                top_songs = [{"title": s["title"]} for s in songs_results if "title" in s]
            # --- FIN DEL FIX ---
            
            if not top_songs: return []

            songs = []
            if not deeper:
                for s in top_songs[:limit]:
                    songs.append(Song(artist=artist_name, title=s["title"], reason=reason))
            else:
                start_idx, end_idx = 10, 25
                deep_cuts_pool = top_songs[start_idx:end_idx]
                
                if len(deep_cuts_pool) >= limit:
                    for s in deep_cuts_pool[:limit]:
                        songs.append(Song(artist=artist_name, title=s["title"], reason=reason + " (Deep cut)"))
                else:
                    for s in top_songs[:limit]:
                        songs.append(Song(artist=artist_name, title=s["title"], reason=reason + " (Fallback)"))
            return songs
        except Exception as e:
            logger.error("Error buscando en YT Music para '%s': %s", artist_name, e)
            return []    
    

    def fetch_songs(self, config: dict) -> List[Song]:
        """Fetch top songs from similar artists (Kikusan interface)."""
        api_key = config["api_key"]
        username = config["username"]
        
        discover_limit = int(config.get("discover_limit", 3))
        library_limit = int(config.get("library_limit", 3))
        new_releases_limit = int(config.get("new_releases_limit", 1))
        tracks_per_artist = int(config.get("tracks_per_artist", 1))
        
        seed_artists_str = config.get("seed_artists", "")
        use_recent = str(config.get("use_recent_listening", "true")).lower() == "true"
        deeper_cuts = str(config.get("deeper_cuts", "false")).lower() == "true"

        if discover_limit == 0 and library_limit == 0 and new_releases_limit == 0:
            logger.info("Límite de artistas o canciones configurado a 0. Saltando búsqueda de nuevas canciones.")
            return []

        exclude_str = config.get("exclude_artists", "")
        exclude_set = {a.strip().lower() for a in exclude_str.split(",") if a.strip()}
        
        only_tags_set = {t.strip().lower() for t in config.get("only_tags", "").split(",") if t.strip()}
        skip_tags_set = {t.strip().lower() for t in config.get("skip_tags", "").split(",") if t.strip()}

        logger.info("Iniciando Mix (Descubrir: %d, Librería: %d, Novedades: %d)", discover_limit, library_limit, new_releases_limit)

        nav_artists = self._get_all_navidrome_artists()
        
        # =====================================================================
        # 1. DESCUBRIMIENTO DIRIGIDO (DISCOVER)
        # Nace de las semillas manuales. Busca similares que NO tengas.
        # =====================================================================
        discover_pool = []
        if discover_limit > 0:
            manual_seeds = [s.strip() for s in seed_artists_str.split(",") if s.strip()]
            if manual_seeds:
                logger.info("Buscando similares a las semillas manuales (Discover)...")
                # Forzamos use_recent=False para que SOLO use las semillas manuales
                scored_candidates = self._get_scored_candidates(api_key, username, use_recent=False, manual_seeds=manual_seeds)
                for artist in scored_candidates:
                    name = artist["artist"]
                    if name.lower() in nav_artists: continue
                    if name.lower() in exclude_set: continue
                    if not self._passes_tag_filters(api_key, name, only_tags_set, skip_tags_set): continue
                    discover_pool.append(artist)
                    # Guardamos un margen extra por si falla YT Music
                    if len(discover_pool) >= discover_limit * 3: break

        # =====================================================================
        # 2. ESCUCHAS RECIENTES (LIBRARY)
        # Nace de tu historial. Filtra SOLO los que SÍ tienes en Navidrome.
        # =====================================================================
        library_pool = []
        if library_limit > 0 and use_recent:
            logger.info("Buscando en tus escuchas recientes artistas que YA TENGAS en Navidrome (Library)...")
            data = self._request_lastfm(api_key, "user.getTopArtists", {"user": username, "period": "1month", "limit": 50})
            recent_artists = data.get("topartists", {}).get("artist", [])
            for a in recent_artists:
                name = a["name"]
                # AQUÍ ESTÁ LA MAGIA: Solo entran si ESTÁN en Navidrome
                if name.lower() not in nav_artists: continue
                if name.lower() in exclude_set: continue
                if not self._passes_tag_filters(api_key, name, only_tags_set, skip_tags_set): continue
                library_pool.append({"artist": name})
                if len(library_pool) >= library_limit * 3: break

        # =====================================================================
        # 3. NOVEDADES
        # =====================================================================
        new_releases_pool = []
        if new_releases_limit > 0:
            new_releases_pool = [r for r in self._get_new_releases(api_key, username, limit=20) if r["artist"].lower() not in exclude_set]

        yt = YTMusic()
        songs = []
        
        def process_pool(pool: List[Dict], limit: int, reason_prefix: str, is_release: bool = False):
            random.shuffle(pool)
            count = 0
            for item in pool:
                if count >= limit: break
                artist_name = item["artist"]
                album_name = item.get("album") if is_release else None
                
                # Buscar sus canciones en YouTube Music
                logger.info("Buscando [%s] %s en YouTube Music...", reason_prefix, artist_name)
                
                tracks = self._get_yt_tracks(yt, artist_name, tracks_per_artist, reason_prefix, deeper_cuts, album_name)
                if tracks:
                    songs.extend(tracks)
                    count += 1
                    
                time.sleep(1) # Evitar bloqueos

        # 4. Ejecutar descargas según las colas construidas
        if discover_limit > 0: process_pool(discover_pool, discover_limit, "Discover")
        if library_limit > 0: process_pool(library_pool, library_limit, "Library")
        if new_releases_limit > 0: process_pool(new_releases_pool, new_releases_limit, "New Release", is_release=True)

        logger.info("Total de canciones encontradas para descargar: %d", len(songs))
        return songs