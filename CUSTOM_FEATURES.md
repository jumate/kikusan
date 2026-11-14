# Características Personalizadas del Fork (Kikusan + Navidrome)

Este documento detalla las modificaciones y nuevas funcionalidades añadidas a este fork de Kikusan. El objetivo principal de estos cambios es lograr una integración profunda con **Navidrome**, evitar la fragmentación de álbumes, automatizar la creación de listas de reproducción basadas en descubrimientos musicales (Flows) y mejorar la resiliencia del sistema frente a fallos de red.

## 1. Nuevo Plugin de Descubrimiento: `lastfm_similar`
Se ha desarrollado un plugin personalizado (`kikusan/plugins/lastfm_similar.py`) que actúa como un motor de recomendación basado en Last.fm.

* **Funcionamiento y Características**:
  1. Obtiene tus artistas más escuchados del último mes (semillas).
  2. **Aleatoriedad**: Mezcla tus artistas top y los similares obtenidos para que el Flow sea diferente en cada ejecución.
  3. **Filtro de historial**: Descarta los artistas recomendados si has escuchado algo de ellos en los **últimos 6 meses**.
  4. **Lista negra**: Permite excluir artistas específicos usando el parámetro `exclude_artists`.
  5. **Deduplicación**: Antes de descargar, consulta la API de Subsonic de Navidrome para comprobar si ya tienes la canción.
  6. **Limpieza Automática (Purge)**: Si se activa `purge_rating: 1`, el plugin lee de la API nativa de Navidrome todas las canciones que hayas puntuado con 1 estrella. Utiliza una **lógica difusa** para comparar nombres de archivo y artistas (soportando diferencias de mayúsculas, caracteres Unicode/japoneses o listas de artistas incompletas) y borra esos archivos físicos del disco. Finalmente, lanza un escaneo automático en Navidrome.

* **Ejemplo de configuración en `cron.yaml`**:
plugins:
  mi-flow-lastfm:
    type: lastfm_similar
    sync: false
    schedule: "0 8 * * 1"
    config:
      api_key: "TU_API_KEY"
      username: "TU_USUARIO"
      similar_artists_limit: 5
      songs_per_artist: 10
      flow_name: "Flow Last.fm"
      exclude_artists: "Nena Daconte, Melendi" 
      purge_rating: 1

*(Nota: Si quieres ejecutar SOLO la limpieza sin descargar música nueva, configura `similar_artists_limit: 0` y `songs_per_artist: 0`).*

## 2. Modo "Flow" (Pure Album) para Plugins
Se ha modificado la lógica de sincronización de plugins (`kikusan/plugins/sync.py`) para permitir que cualquier plugin agrupe sus descargas como un único álbum (compilación).

* **Comportamiento**:
  * Descarga todas las canciones en una carpeta común (ej. `Flow Last.fm`) dentro de la raíz de la biblioteca.
  * Aplica los tags automáticamente: `Album`, `AlbumArtist` ("Varios Artistas"), `Compilation` ("1") y `Original Album`.
  * Genera un archivo `.m3u` con el nombre del Flow.
  * Limpia los archivos temporales y las carpetas vacías tras mover las descargas.

## 3. Soporte Mejorado para Deezer (Playlists y Álbumes)
Se ha extendido el soporte nativo de Deezer para aceptar tanto listas de reproducción como álbumes (`/album/`).

* **Novedades**:
  * Descarga la carátula en alta resolución desde Deezer y la guarda como `folder.jpg` en la carpeta del álbum para Navidrome y Primuse.
  * La interfaz web (`index.html`) permite pegar URLs de álbumes de Deezer en la pestaña de "Playlists".

## 4. Procesamiento de Imágenes y Metadatos (Title Case)
* **Carátulas Cuadradas**: Se utiliza `Pillow` para recortar las carátulas de YouTube a un cuadrado perfecto de 500x500 píxeles antes de incrustarlas.
* **Formato Title Case**: Los nombres de artistas, álbumes y carpetas se formatean a "Title Case" para mantener la coherencia visual.

## 5. Resiliencia y Descargas con Doble Autenticación
* **LRCLIB (Letras)**: Implementación de `tenacity` y `retryhttp` para manejar errores `503`. Registro de App-ID para evitar límites de peticiones.
* **Doble Autenticación en YouTube**: Uso de `browser.json` (cuenta principal) para leer listas privadas y `cookies.txt` (cuenta secundaria) para realizar las descargas masivas de forma segura.

## 6. Integración con Navidrome
* **Evitar fragmentación**: El etiquetado `Compilation=1` y `AlbumArtist=Varios Artistas` evita que Navidrome fragmente las listas en cientos de carpetas.
* **Ignorar carpetas temporales**: Uso de `.ndignore` en `recent_download`.
* **Protección de archivos**: Kikusan consulta a Navidrome para proteger canciones favoritas antes de borrarlas en los modos `sync`.
