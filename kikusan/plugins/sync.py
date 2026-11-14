"""Plugin synchronization logic."""

import logging
import shutil
import os
from datetime import datetime
from pathlib import Path

from kikusan.download import download
from kikusan.playlist import rebuild_m3u
from kikusan.plugins.base import Plugin, PluginConfig, Song, SyncResult
from kikusan.plugins.state import (
    PluginState,
    PluginTrackState,
    get_state_dir,
    load_plugin_state,
    save_plugin_state,
)
from kikusan.reference_checker import get_navidrome_protection_cache, is_safe_to_delete
from kikusan.search import search, get_song_metadata
from kikusan.unavailable import is_on_cooldown, is_unavailable_error, record_unavailable

logger = logging.getLogger(__name__)


def sync_plugin_instance(
    plugin: Plugin,
    config: PluginConfig,
    sync_mode: bool,
) -> SyncResult:
    """Sync a plugin instance."""
    from kikusan.config import get_config as get_app_config
    app_config = get_app_config()

    logger.info("Starting plugin sync: %s (%s)", config.name, plugin.name)
    state_dir = get_state_dir(app_config.data_dir)

    flow_name = config.config.get("flow_name")
    playlist_name = flow_name if flow_name else config.name
    purge_rating = config.config.get("purge_rating")

    try:
        current_songs = plugin.fetch_songs(config.config)
        logger.info("Plugin returned %d songs", len(current_songs))

        # NUEVO: Si no hay canciones pero hay purge_rating, no abortamos, dejamos que pase al borrado.
        if not current_songs and not purge_rating:
            logger.warning("No songs returned by plugin and no purge_rating configured. Exiting.")
            return SyncResult(plugin_name=config.name, downloaded=0, skipped=0, failed=0, errors=[])

        state = load_plugin_state(state_dir, config.name)
        if not state:
            state = PluginState(plugin_name=config.name, plugin_type=plugin.name, source_url=config.config.get("url"), last_check=datetime.now().isoformat(), tracks=[])

        new_songs, removed_tracks = _compare_songs(current_songs, state)
        logger.info("Changes: %d new, %d removed", len(new_songs), len(removed_tracks))

        download_result = {"downloaded": 0, "skipped": 0, "failed": 0, "errors": []}
        if new_songs:
            download_result = _download_songs(new_songs, config, state, plugin=plugin, flow_name=flow_name)

        deleted_count = 0
        if sync_mode and removed_tracks:
            deleted_count = _remove_tracks(removed_tracks, state, config.download_dir, app_config.data_dir)

        # NUEVO: Purgar canciones con 1 estrella en Navidrome y lanzar escaneo si se borra algo
        purged_count = 0
        if flow_name and purge_rating and hasattr(plugin, "get_disliked_songs"):
            purged_count = _purge_disliked_tracks(plugin, state, int(purge_rating), config)
            
            if purged_count > 0 and hasattr(plugin, "trigger_navidrome_scan"):
                plugin.trigger_navidrome_scan()

        # Actualizar M3U (después de purgar, para que no se incluyan las borradas)
        _update_m3u(playlist_name, state, config.download_dir)

        state.last_check = datetime.now().isoformat()
        if "url" in config.config:
            state.source_url = config.config["url"]
        save_plugin_state(state_dir, state)

        result = SyncResult(
            plugin_name=config.name,
            downloaded=download_result["downloaded"],
            skipped=download_result["skipped"],
            failed=download_result["failed"],
            errors=download_result["errors"],
        )

        logger.info("Plugin sync completed: %d downloaded, %d skipped, %d deleted, %d failed", result.downloaded, result.skipped, deleted_count + purged_count, result.failed)
        return result

    except Exception as e:
        logger.error("Plugin sync failed: %s", e)
        return SyncResult(plugin_name=config.name, downloaded=0, skipped=0, failed=1, errors=[str(e)])
    finally:
        _cleanup_recent_download(config.download_dir)


def _purge_disliked_tracks(plugin: Plugin, state: PluginState, rating_threshold: int, config: PluginConfig) -> int:
    """Busca en el estado las canciones y borra los archivos físicos que tengan la calificación indicada en Navidrome."""
    logger.info("Obteniendo la lista de canciones marcadas con %d estrellas desde Navidrome...", rating_threshold)
    
    disliked_songs = plugin.get_disliked_songs(rating_threshold)
    
    if not disliked_songs:
        logger.info("No se han encontrado canciones con %d estrellas en Navidrome.", rating_threshold)
        return 0

    logger.info("Navidrome ha reportado %d canciones con %d estrellas. Comprobando si alguna está en el Flow...", len(disliked_songs), rating_threshold)
    deleted_count = 0
    
    for track in state.tracks:
        file_path = Path(track.file_path)
        
        if not file_path.exists():
            continue
            
        # Usamos el nombre del archivo sin la extensión como principal fuente de verdad
        file_name = file_path.stem.lower().strip()
        t_title = track.title.lower().strip()
        t_artist = track.artist.lower().strip()
        
        match = False
        for d_title, d_artist in disliked_songs:
            # Comprobación 1: El nombre del archivo coincide con el título de Navidrome
            file_match = (d_title in file_name) or (file_name in d_title)
            
            # Comprobación 2: Los tags coinciden
            title_match = (t_title == d_title) or (t_title in d_title) or (d_title in t_title)
            
            # Comprobación laxa de artista: si Kikusan no supo el artista (Unknown, Varios), lo ignoramos
            # o comprobamos si una parte del nombre del artista está en el otro.
            artist_match = (t_artist in d_artist) or (d_artist in t_artist) or (t_artist in ["unknown", "unknown artist", "varios artistas", ""])
            
            if (file_match or title_match) and artist_match:
                match = True
                logger.info("  [MATCH] Kikusan: '%s' (Artista: %s) <==> Navidrome: '%s' (Artista: %s)", file_path.name, t_artist, d_title, d_artist)
                break
        
        if match:
            logger.info("Borrando archivo (1 estrella): %s", file_path.name)
            try:
                file_path.unlink()
                lrc_path = file_path.with_suffix(".lrc")
                if lrc_path.exists():
                    lrc_path.unlink()
                deleted_count += 1
            except OSError as e:
                logger.error("No se pudo borrar el archivo %s: %s", file_path, e)

    if deleted_count > 0:
        logger.info("Se han borrado físicamente %d archivos marcados con %d estrellas.", deleted_count, rating_threshold)
    else:
        logger.info("Ninguna de las canciones del Flow está marcada con %d estrellas en Navidrome o ya no existen en el disco.", rating_threshold)
        
    return deleted_count
    
    
def _cleanup_recent_download(download_dir: Path):
    """Intenta borrar la carpeta recent_download si está vacía."""
    recent_dir = download_dir / "recent_download"
    if recent_dir.exists():
        try:
            for root, dirs, files in os.walk(recent_dir, topdown=False):
                for name in dirs:
                    try:
                        os.rmdir(os.path.join(root, name))
                    except OSError:
                        pass
            recent_dir.rmdir()
            logger.info("Carpeta 'recent_download' eliminada por estar vacía.")
        except OSError:
            pass


def _compare_songs(current: list[Song], state: PluginState) -> tuple[list[Song], list[PluginTrackState]]:
    """Compare current songs with state."""
    current_keys = {song.cache_key for song in current}
    state_keys = {track.cache_key for track in state.tracks}
    new_keys = current_keys - state_keys
    removed_keys = state_keys - current_keys
    new_songs = [s for s in current if s.cache_key in new_keys]
    removed_tracks = [t for t in state.tracks if t.cache_key in removed_keys]
    return new_songs, removed_tracks


def _download_songs(
    songs: list[Song],
    config: PluginConfig,
    state: PluginState,
    plugin: Plugin,
    flow_name: str | None = None,
) -> dict:
    """Download songs and update state."""
    downloaded, skipped, failed = 0, 0, 0
    errors = []

    from kikusan.config import get_config
    app_config = get_config()
    cooldown_hours = app_config.unavailable_cooldown_hours

    for i, song in enumerate(songs, 1):
        logger.info("[%d/%d] Searching: %s - %s", i, len(songs), song.artist, song.title)

        if any(t.cache_key == song.cache_key for t in state.tracks):
            logger.info("  Already downloaded, skipping")
            skipped += 1
            continue           

        if hasattr(plugin, "_is_already_in_navidrome") and plugin._is_already_in_navidrome(song.artist, song.title):
            skipped += 1
            continue

        video_id = None
        try:
            query = f"{song.artist} - {song.title}"
            results = search(query, limit=1)

            if not results:
                logger.warning("  Not found on YouTube Music")
                failed += 1
                errors.append(f"Not found: {song.artist} - {song.title}")
                continue

            yt_track = results[0]
            video_id = yt_track.video_id

            if is_on_cooldown(app_config.data_dir, video_id, cooldown_hours):
                logger.info("  Skipping (unavailable cooldown): %s - %s", yt_track.artist, yt_track.title)
                skipped += 1
                continue

            logger.info("  Found: %s - %s", yt_track.artist, yt_track.title)

            meta = get_song_metadata(video_id)
            original_album = meta.album if meta else yt_track.album

            org_mode = config.organization_mode
            album_artist = None
            custom_album_name = None
            is_compilation = False
            
            if flow_name:
                org_mode = "pure_album"
                album_artist = "Varios Artistas"
                custom_album_name = flow_name
                is_compilation = True

            audio_path = download(
                video_id=video_id,
                output_dir=config.download_dir,
                audio_format=config.audio_format,
                filename_template=config.filename_template,
                fetch_lyrics=True,
                organization_mode=org_mode,
                use_primary_artist=config.use_primary_artist,
                apply_replaygain=app_config.replaygain,
                album_artist=album_artist,
                nested_folders=True,
                custom_album_name=custom_album_name,
                is_compilation=is_compilation,
                original_album=original_album,
            )

            if audio_path:
                if flow_name:
                    try:
                        target_dir = Path(config.download_dir) / flow_name
                        target_dir.mkdir(parents=True, exist_ok=True)
                        new_path = target_dir / audio_path.name
                        
                        shutil.move(str(audio_path), str(new_path))
                        
                        old_lrc = audio_path.with_suffix(".lrc")
                        if old_lrc.exists():
                            shutil.move(str(old_lrc), str(new_path.with_suffix(".lrc")))
                        
                        try:
                            audio_path.parent.rmdir()
                            audio_path.parent.parent.rmdir()
                        except OSError:
                            pass

                        audio_path = new_path
                    except Exception as e:
                        logger.warning("  No se pudo mover el archivo a la raíz: %s", e)

                track_state = PluginTrackState(
                    cache_key=song.cache_key,
                    artist=song.artist,
                    title=song.title,
                    file_path=str(audio_path),
                    downloaded_at=datetime.now().isoformat(),
                    video_id=video_id,
                )
                state.tracks.append(track_state)
                downloaded += 1

        except Exception as e:
            logger.error("  Failed: %s", e)
            failed += 1
            errors.append(f"{song.artist} - {song.title}: {e}")
            if video_id and is_unavailable_error(str(e)):
                record_unavailable(app_config.data_dir, video_id, str(e), title=song.title, artist=song.artist)

    return {"downloaded": downloaded, "skipped": skipped, "failed": failed, "errors": errors}


def _remove_tracks(tracks: list[PluginTrackState], state: PluginState, download_dir: Path, data_dir: Path | None = None) -> int:
    """Remove tracks from filesystem and state."""
    if data_dir is None:
        from kikusan.config import get_config
        data_dir = get_config().data_dir
    deleted, skipped = 0, 0
    navidrome_cache = get_navidrome_protection_cache()

    for track in tracks:
        logger.info("Removing: %s - %s", track.artist, track.title)
        file_path = Path(track.file_path)

        if file_path.exists():
            if not is_safe_to_delete(file_path, data_dir, current_plugin_name=state.plugin_name, navidrome_cache=navidrome_cache):
                logger.info("Skipping deletion of %s (referenced by other playlists/plugins)", file_path.name)
                skipped += 1
                state.tracks = [t for t in state.tracks if t.cache_key != track.cache_key]
                continue
            try:
                file_path.unlink()
                deleted += 1
                lrc_path = file_path.with_suffix(".lrc")
                if lrc_path.exists(): lrc_path.unlink()
            except Exception as e:
                logger.error("Failed to delete %s: %s", file_path, e)
                continue

        state.tracks = [t for t in state.tracks if t.cache_key != track.cache_key]

    if skipped > 0:
        logger.info("Skipped deletion of %d file(s) referenced by other sources", skipped)
    return deleted


def _update_m3u(name: str, state: PluginState, download_dir: Path) -> None:
    """Update M3U playlist."""
    if not state.tracks: return

    file_paths = []
    for track in state.tracks:
        path = Path(track.file_path)
        if path.exists():
            file_paths.append(path)

    try:
        rebuild_m3u(file_paths, name, download_dir)
    except Exception as e:
        logger.error("Failed to update M3U: %s", e)