# Características Personalizadas del Fork (Kikusan + Navidrome)

Este documento detalla las modificaciones y nuevas funcionalidades añadidas a este fork de Kikusan. El objetivo principal de estos cambios es lograr una integración profunda con **Navidrome**, evitar la fragmentación de álbumes, automatizar la creación de listas de reproducción basadas en descubrimientos musicales (Flows) y mejorar la resiliencia del sistema frente a fallos de red.

## 1. Nuevo Plugin de Descubrimiento: `lastfm_similar`
Se ha desarrollado un plugin personalizado (`kikusan/plugins/lastfm_similar.py`) que actúa como un motor de recomendación basado en Last.fm.

* **Funcionamiento**:
  1. Obtiene tus artistas más escuchados del último mes (semillas).
  2. Busca artistas similares a esas semillas en la API de Last.fm.
  3. **Filtro de historial**: Descarta los artistas recomendados si has escuchado algo de ellos en los **últimos 6 meses** (garantizando que sean descubrimientos realmente nuevos).
  4. Busca las canciones más populares de los artistas finales en YouTube Music.

* **Configuración**: Para usarlo, añade el plugin `lastfm_similar` a tu `cron.yaml` con tus credenciales de Last.fm y el parámetro `flow_name` para agrupar las canciones.

* **Ejemplo de configuración en `cron.yaml`**:
```yaml
plugins:
  mi-flow-lastfm:
    type: lastfm_similar
    sync: false
    schedule: "0 8 * * 1" # Todos los lunes a las 8:00
    config:
      api_key: "TU_API_KEY"
      username: "TU_USUARIO"
      similar_artists_limit: 5
      songs_per_artist: 10
      flow_name: "Flow Last.fm"


## 2. Modo "Flow" (Pure Album) para Plugins
Se ha modificado la lógica de sincronización de plugins (`kikusan/plugins/sync.py`) para permitir que cualquier plugin agrupe sus descargas como un único álbum (compilación) si se le pasa el parámetro `flow_name`.

* **Comportamiento**:
  * Descarga todas las canciones en una carpeta común (ej. `Flow Last.fm`) dentro de la raíz de la biblioteca.
  * Aplica los tags automáticamente: `Album` (nombre del Flow), `AlbumArtist` ("Varios Artistas"), `Compilation` ("1") y `Original Album` (el disco original de cada canción).
  * Genera un archivo `.m3u` con el nombre del Flow que contiene todas las canciones.
  * Limpia los archivos temporales (como `.webp`) y las carpetas vacías tras mover las descargas.

## 3. Soporte Mejorado para Deezer (Playlists y Álbumes)
Se ha extendido el soporte nativo de Deezer para aceptar tanto listas de reproducción como álbumes.

* **Novedades**:
  * El sistema detecta URLs de álbumes (`/album/`) y consulta la API de Deezer para extraer el nombre real del disco.
  * **Descarga de portadas**: Se descarga la carátula en alta resolución desde Deezer y se guarda automáticamente como `folder.jpg` en la carpeta del álbum para que Navidrome y Primuse la muestren.
  * La interfaz web (`index.html`) ahora permite pegar URLs de álbumes de Deezer en la pestaña de "Playlists".

## 4. Procesamiento de Imágenes y Metadatos (Title Case)
* **Carátulas Cuadradas**: Se utiliza la librería `Pillow` para recortar las carátulas descargadas de YouTube (que suelen ser apaisadas) a un cuadrado perfecto de 500x500 píxeles antes de incrustarlas.
* **Formato Title Case**: Los nombres de los artistas, álbumes y carpetas se formatean automáticamente a "Title Case" (ej. `BURNING` pasa a `Burning`), manteniendo la coherencia visual.

## 5. Resiliencia y Descargas con Doble Autenticación
* **LRCLIB (Letras)**: Se ha implementado `tenacity` y `retryhttp` para manejar los errores `503 Service Unavailable`. El sistema respeta el encabezado `Retry-After` y reintenta con espera exponencial. Además, se ha registrado una App-ID para evitar bloqueos por límite de peticiones.
* **Doble Autenticación en YouTube**:
  * `YTMUSIC_COOKIE_FILE` (`browser.json`): Usa las credenciales de la **cuenta principal** para leer listas y Mixes privados, evitando bloqueos de IP al no usarla para descargar.
  * `YT_DLP_COOKIE_FILE` (`cookies.txt`): Usa las credenciales de una **cuenta secundaria** para realizar las descargas masivas, protegiendo la cuenta principal.

## 6. Integración con Navidrome
* **Evitar fragmentación**: Gracias al etiquetado `Compilation=1` y `AlbumArtist=Varios Artistas`, Navidrome agrupa todas las canciones de una lista bajo un solo álbum, evitando que cree una carpeta por cada artista.
* **Ignorar carpetas temporales**: Se puede excluir la carpeta `recent_download` de los escaneos de Navidrome añadiendo un archivo oculto `.ndignore`.
* **Protección de archivos**: Kikusan consulta a Navidrome antes de borrar archivos locales si se usa el modo `sync: true`, protegiendo las canciones que tengas marcadas con favoritos (estrellas).
