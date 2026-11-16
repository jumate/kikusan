# Documentación Técnica Maestra: Fork de Kikusan + Aurral Flow + Navidrome

## 1. Resumen del Proyecto
Este repositorio es un fork personalizado de **Kikusan** (un descargador y organizador de música de YouTube Music para Navidrome). El objetivo principal de este fork es implementar un sistema de descubrimiento musical híbrido y altamente configurable, inspirado en la interfaz de "Discover Weekly" de **Aurral**. 

El sistema combina el historial de Last.fm, la biblioteca local (Navidrome), Deezer y YouTube Music para crear Flows que mezclan música nueva con canciones ocultas de artistas que el usuario ya posee, manteniendo una biblioteca limpia, bien etiquetada y sin fragmentación de álbumes.

---

## 2. Infraestructura y Despliegue (Docker)

El proyecto se ejecuta mediante Docker Compose, utilizando el gestor de paquetes `uv` para Python.

### `docker-compose.yml` (Configuración Base)
*   **Contenedor:** `kikusan-cron`
*   **Comando principal:** `uv run kikusan cron --config /config/cron.yaml`
*   **Volúmenes:**
    *   `/mnt/Resto/MusicaNew/canciones:/music/canciones` (Almacenamiento de música)
    *   `./kikusan_config:/config` (Configuración, cookies, etc.)
*   **Variables de Entorno Clave:**
    *   **YouTube:** `YT_DLP_COOKIE_FILE=/config/cookies.txt`, `KIKUSAN_COOKIE_MODE=always` (Evita bloqueos).
    *   **Audio/Tags:** `KIKUSAN_AUDIO_FORMAT=opus`, `KIKUSAN_REPLAYGAIN=true`, `KIKUSAN_ORGANIZATION_MODE=flat`, `KIKUSAN_USE_PRIMARY_ARTIST=true`.
    *   **Navidrome:** `NAVIDROME_URL`, `NAVIDROME_USER`, `NAVIDROME_PASSWORD`, `NAVIDROME_ADMIN_USER`, `NAVIDROME_ADMIN_PASSWORD`.
    *   **Límites:** `KIKUSAN_UNAVAILABLE_MAX_ATTEMPTS=3`, `KIKUSAN_UNAVAILABLE_REPORT_FILE`.
    *   **Letras:** `LRCLIB_APP_ID`, `LRCLIB_API_KEY`, `KIKUSAN_LYRICS_ENABLED=false`.

---

## 3. Motor de Descubrimiento: `lastfm_similar.py` (Estilo Aurral)

Es el motor de recomendación principal. Ha sido reescrito para emular la lógica de la interfaz de Aurral.

### Lógica Híbrida (Discover vs Library)
*   **`discover_pool`:** Artistas recomendados por Last.fm que **NO** existen en la base de datos de Navidrome.
*   **`library_pool`:** Artistas que el usuario escucha recientemente y que **SÍ** existen en Navidrome (se buscan canciones no descargadas aún).
*   **`new_releases_pool`:** Novedades obtenidas vía `user.getNewReleases`.

### Motor de Descubrimiento Aleatorio (`_get_scored_candidates`)
1.  **Semillas:** Coge el Top 100 de artistas del último mes de Last.fm. Los mezcla (shuffle) y selecciona 15 al azar. Le suma los `seed_artists` manuales.
2.  **Similares:** Para cada semilla, busca 30 artistas similares. Los mezcla y coge los 10 mejores.
3.  **Filtro Temporal:** Filtra contra los artistas escuchados en los **últimos 3 meses** (`period: "3month"` en `user.getTopArtists`). Evita recomendar artistas que ya se escuchan en bucle.
4.  **Puntuación:** Los artistas recomendados por múltiples semillas obtienen mayor puntuación.

### Funciones Extra y Fixes
*   **Filtros de Etiquetas:** Implementa `only_tags` (inclusión) y `skip_tags` (exclusión) consultando `artist.getTopTags` en Last.fm.
*   **Deeper Cuts:** Ignora el Top 10 de YouTube Music y extrae canciones entre las posiciones 10 y 25. Si falla, hace fallback al Top 10.
*   **Fallback de YTMusic:** Si `yt.get_artist()` falla (error `Unable to find 'header'` en la API), hace una búsqueda directa por canciones (`yt.search(..., filter="songs")`).

---

## 4. Sincronización y Limpieza (`sync.py`)

*   **Inyección de Motivo (`reason`):** El plugin inyecta la propiedad `reason` en cada objeto `Song` (ej. "Discover", "Library", "New Release").
*   **Purga de 1 Estrella (`_purge_disliked_tracks`):**
    *   Conecta a la API nativa de Navidrome (`/auth/login` y `/api/song`).
    *   Busca canciones con `rating == 1`.
    *   Localiza el archivo físico en la carpeta del Flow usando **lógica difusa** (soporta diferencias de mayúsculas, Unicode o listas de artistas incompletas) y lo borra junto con su `.lrc`.
    *   Lanza un escaneo completo (`startScan`) en Navidrome vía API de Subsonic.
    *   *Nota:* El estado del plugin se deja intacto para que Kikusan no vuelva a descargar esa canción en el futuro.
*   **Limpieza Forzosa de Residuos (`_cleanup_recent_download`):**
    *   **FIX CRÍTICO:** Se reemplazó `os.rmdir` por `shutil.rmtree` para borrar forzosamente la carpeta `recent_download`. Esto elimina los archivos huérfanos `.webp`, `.lrc` y `.nfo` que `yt-dlp` deja atrás.

---

## 5. Motor de Descarga y Etiquetado (`download.py`)

*   **Soporte para `comment`:** Se añadió el parámetro `comment` a la firma de `download()` y a `_apply_pure_album_tags()`.
*   **Escritura de metadatos:** Usa `mutagen` para escribir el motivo de la recomendación (`Discover`, `Library`) en la etiqueta `COMMENT` del archivo final (visible en Navidrome, Mp3tag, etc.).
*   **Gestión de Portadas:** Tras embeber la imagen en el archivo `.opus` usando `_embed_cover_art()`, se eliminan las miniaturas originales (`.webp`).

---

## 6. Integración con Deezer, Imágenes y Autenticación

*   **Soporte Deezer:** El sistema acepta tanto listas de reproducción como álbumes (`/album/`) de Deezer.
*   **Carátulas Cuadradas:** Se utiliza `Pillow` para recortar las carátulas a un cuadrado perfecto de 500x500 píxeles antes de incrustarlas. En Deezer, se descarga en alta resolución y se guarda como `folder.jpg`.
*   **Formato Title Case:** Los nombres de artistas, álbumes y carpetas se formatean a "Title Case" para mantener la coherencia visual.
*   **Doble Autenticación YouTube:** Uso de `browser.json` (cuenta principal) para leer listas privadas y `cookies.txt` (cuenta secundaria) para realizar las descargas.
*   **LRCLIB (Letras):** Implementación de `tenacity` y `retryhttp` para manejar errores `503`.

---

## 7. Integración con Navidrome y Modo "Flow"

*   **Modo Pure Album:** Los Flows se agrupan en una carpeta común (ej. `Flow Last.fm`). Se aplican los tags `Album`, `AlbumArtist` ("Varios Artistas"), `Compilation` ("1") y `Original Album`.
*   **Evitar fragmentación:** El etiquetado `Compilation=1` y `AlbumArtist=Varios Artistas` evita que Navidrome fragmente las listas en cientos de carpetas.
*   **Protección de archivos:** Kikusan consulta a Navidrome para proteger canciones favoritas antes de borrarlas en los modos `sync`.
*   **Ignorar carpetas temporales:** Uso de `.ndignore` en `recent_download`.

---

## 8. Ejemplo de Configuración (`cron.yaml`)

```yaml
plugins:
  mi-flow-lastfm:
    type: lastfm_similar
    sync: false
    schedule: "0 9 * * sat"
    config:
      api_key: "xxx"
      username: "xxxx"
      flow_name: "Flow Last.Fm"
      
      # Lógica Aurral
      discover_limit: 3          # Canciones de artistas nuevos
      library_limit: 2           # Canciones de artistas que ya tienes
      new_releases_limit: 0      # Novedades (0 desactiva)
      
      # Semillas y filtros
      seed_artists: "Dire Straits, Pink Floyd, Marea" 
      use_recent_listening: true # Activa el Top 100 + Filtro de 3 meses
      deeper_cuts: false         # Top 10 vs Posiciones 10-25
      tracks_per_artist: 1
      
      # Etiquetas y exclusiones
      only_tags: "rock español, indie"
      skip_tags: "pop, reggaeton"
      exclude_artists: "Nena Daconte"
      purge_rating: 1
```

---

## 9. Comandos de Desarrollo y Pruebas

**Para probar el plugin manualmente sin esperar al cron (Usando JSON):**
```bash
sudo docker compose exec kikusan-cron uv run kikusan plugins run lastfm_similar --config '{"api_key": "xxx", "username": "xxxx", "flow_name": "Flow Last.Fm", "discover_limit": 3, "library_limit": 2, "new_releases_limit": 0, "tracks_per_artist": 1, "seed_artists": "Marea", "use_recent_listening": true, "deeper_cuts": false, "only_tags": "", "skip_tags": "", "exclude_artists": "", "purge_rating": 1}'
```

**Para aplicar cambios en el código Python (Reconstruir contenedor):**
```bash
sudo docker compose build kikusan-cron
sudo docker compose up -d kikusan-cron
```

**Para ver los logs en tiempo real:**
```bash
docker logs -f kikusan-cron
```

---

## 10. Historial de Bugs Resueltos
1.  **Archivos `.webp` huérfanos:** La carpeta `recent_download` no se borraba porque `os.rmdir` falla si hay archivos dentro. **Solución:** `shutil.rmtree`.
2.  **Error `unexpected keyword argument 'comment'`:** Kikusan ignoraba el parámetro `comment` al descargar. **Solución:** Actualizar `download.py` y `sync.py`.
3.  **Error `Unable to find 'header'` en YouTube Music:** La API devolvía un JSON con una estructura inesperada para ciertos artistas (ej. Antonio Orozco). **Solución:** Bloque `try/except` con búsqueda directa por canciones (`filter="songs"`).
4.  **Descargas duplicadas (Falsos positivos):** Algunos artistas se descargaban dos veces si el título variaba ligeramente. **Solución:** (Pendiente) Ajustar el `cache_key` en `base.py`.
