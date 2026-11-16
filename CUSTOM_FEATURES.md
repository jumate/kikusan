# Características Personalizadas del Fork (Kikusan + Navidrome)

Este documento detalla las modificaciones y nuevas funcionalidades añadidas a este fork de Kikusan. El objetivo principal de estos cambios es lograr una integración profunda con **Navidrome**, evitar la fragmentación de álbumes, automatizar la creación de listas de reproducción basadas en descubrimientos musicales (Flows) y mejorar la resiliencia del sistema frente a fallos de red.

## 1. Motor de Descubrimiento Híbrido: `lastfm_similar` (Estilo Aurral)
Se ha reescrito el plugin personalizado (`kikusan/plugins/lastfm_similar.py`) para emular la lógica de recomendación de la interfaz de Aurral, permitiendo un control granular sobre qué se descarga.

* **Funcionamiento y Características**:
  1. **Lógica Híbrida (Discover vs Library)**: Separa las recomendaciones cruzando los datos con Navidrome:
     * `discover_limit`: Descarga canciones de artistas recomendados que **NO** tienes en tu biblioteca.
     * `library_limit`: Busca canciones no descargadas de artistas que **SÍ** tienes, basándose en tus escuchas recientes.
     * `new_releases_limit`: Busca lanzamientos recientes de artistas afines usando `user.getNewReleases`.
  2. **Descubrimiento Aleatorio (Semillas)**: Coge tu Top 100 de artistas del último mes, los mezcla (shuffle), elige 15 al azar y los combina con tus `seed_artists` manuales.
  3. **Filtro de Historial (3 meses)**: Para evitar recomendar artistas que ya escuchas en bucle, filtra los candidatos contra tu historial de los **últimos 3 meses** (`period: "3month"`).
  4. **Filtros de Etiquetas (Tags)**: Permite incluir (`only_tags`) o excluir (`skip_tags`) artistas según sus etiquetas musicales en Last.fm.
  5. **Deeper Cuts**: Si se activa, ignora el Top 10 de YouTube Music y extrae canciones entre las posiciones 10 y 25 para evitar los grandes éxitos. Si el artista no tiene suficientes, hace fallback al Top 10.
  6. **Deduplicación**: Antes de descargar, consulta la API de Subsonic de Navidrome para comprobar si ya tienes la canción.
  7. **Limpieza Automática (Purge)**: Si se activa `purge_rating: 1`, lee de la API nativa de Navidrome todas las canciones puntuadas con 1 estrella. Utiliza una **lógica difusa** para comparar nombres de archivo y artistas y borra esos archivos físicos del disco, lanzando un escaneo automático en Navidrome después.

* **Ejemplo de configuración en `cron.yaml`**:
```yaml
plugins:
  mi-flow-lastfm:
    type: lastfm_similar
    sync: false
    schedule: "0 8 * * 1"
    config:
      api_key: "TU_API_KEY"
      username: "TU_USUARIO"
      flow_name: "Flow Last.fm"
      
      # Lógica Aurral
      discover_limit: 3          # Canciones de artistas nuevos
      library_limit: 2           # Canciones de artistas que ya tienes
      new_releases_limit: 0      # Novedades (0 desactiva)
      
      # Semillas y filtros
      seed_artists: "Fito & Fitipaldis, Marea"
      use_recent_listening: true # Activa el Top 100 + Filtro de 3 meses
      deeper_cuts: false         # Top 10 vs Posiciones 10-25
      tracks_per_artist: 1
      
      # Etiquetas y exclusiones
      only_tags: "rock español, indie"
      skip_tags: "pop, reggaeton"
      exclude_artists: "Nena Daconte" 
      purge_rating: 1
```

## 2. Modo "Flow" (Pure Album) y Etiquetado de Motivos
Se ha modificado la lógica de sincronización de plugins (`kikusan/plugins/sync.py`) y descarga (`kikusan/download.py`) para mejorar la experiencia de los Flows.

* **Comportamiento**:
  * Descarga todas las canciones en una carpeta común (ej. `Flow Last.fm`) dentro de la raíz de la biblioteca.
  * Aplica los tags automáticamente: `Album`, `AlbumArtist` ("Varios Artistas"), `Compilation` ("1") y `Original Album`.
  * **Nuevo Tag `COMMENT`**: Inyecta el motivo de la recomendación (ej. "Discover", "Library", "New Release") en la etiqueta `COMMENT` del archivo. Esto permite saber por qué Kikusan eligió esa canción directamente desde Navidrome o reproductores como Mp3tag.
  * Genera un archivo `.m3u` con el nombre del Flow.
  * **Limpieza de Residuos (Fix)**: Se ha reemplazado `os.rmdir` por `shutil.rmtree` para borrar forzosamente la carpeta temporal `recent_download`. Esto soluciona el problema de los archivos huérfanos `.webp` (carátulas), `.lrc` (letras) y `.nfo` que dejaba YouTube Music y que impedían borrar la carpeta.

## 3. Soporte Mejorado para Deezer (Playlists y Álbumes)
Se ha extendido el soporte nativo de Deezer para aceptar tanto listas de reproducción como álbumes (`/album/`).

* **Novedades**:
  * Descarga la carátula en alta resolución desde Deezer y la guarda como `folder.jpg` en la carpeta del álbum para Navidrome y Primuse.
  * La interfaz web (`index.html`) permite pegar URLs de álbumes de Deezer en la pestaña de "Playlists".

## 4. Procesamiento de Imágenes y Metadatos (Title Case)
* **Carátulas Cuadradas**: Se utiliza `Pillow` para recortar las carátulas de YouTube a un cuadrado perfecto de 500x500 píxeles antes de incrustarlas.
* **Formato Title Case**: Los nombres de artistas, álbumes y carpetas se formatean a "Title Case" para mantener la coherencia visual.
* **Gestión de Portadas**: Tras embeber la imagen en el archivo `.opus`, se eliminan las miniaturas originales (`.webp`) descargadas temporalmente.

## 5. Resiliencia y Descargas con Doble Autenticación
* **LRCLIB (Letras)**: Implementación de `tenacity` y `retryhttp` para manejar errores `503`. Registro de App-ID para evitar límites de peticiones.
* **Doble Autenticación en YouTube**: Uso de `browser.json` (cuenta principal) para leer listas privadas y `cookies.txt` (cuenta secundaria) para realizar las descargas masivas de forma segura.
* **Fallback de YouTube Music (Fix)**: Debido a que la API de YouTube a veces devuelve estructuras JSON inesperadas para ciertos artistas (causando el error `Unable to find 'header'`), se ha implementado un bloque `try/except`. Si falla la obtención del perfil del artista, el sistema hace una búsqueda directa por canciones (`yt.search(..., filter="songs")`) como plan B.

## 6. Integración con Navidrome
* **Evitar fragmentación**: El etiquetado `Compilation=1` y `AlbumArtist=Varios Artistas` evita que Navidrome fragmente las listas en cientos de carpetas.
* **Ignorar carpetas temporales**: Uso de `.ndignore` en `recent_download`.
* **Protección de archivos**: Kikusan consulta a Navidrome para proteger canciones favoritas antes de borrarlas en los modos `sync`.
