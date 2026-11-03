"""Download functionality using yt-dlp."""

import logging
from pathlib import Path

import yt_dlp

from kikusan.config import DEFAULT_FILENAME_TEMPLATE, MAX_FILENAME_BYTES, get_config
from kikusan.lyrics import get_lyrics_for_video, save_lyrics
from kikusan.tags import write_multi_artist_tags
from kikusan.unavailable import is_on_cooldown, is_unavailable_error, record_unavailable
from kikusan.yt_dlp_wrapper import extract_info_with_retry
from kikusan.tagging import _embed_cover_art  # <-- Importamos nuestra función

logger = logging.getLogger(__name__)

class UnavailableCooldownError(Exception):
    pass

def _sanitize_path_component(name: str, max_bytes: int = MAX_FILENAME_BYTES) -> str:
    invalid_chars = '<>:"|?*'
    for char in invalid_chars:
        name = name.replace(char, "")
    name = name.replace("/", "-").replace("\\", "-")
    name = name.strip(". ")
    name = _truncate_to_bytes(name, max_bytes)
    return name or "Unknown"

def _truncate_to_bytes(text: str, max_bytes: int) -> str:
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    encoded = b""
    for char in text:
        char_bytes = char.encode("utf-8")
        if len(encoded) + len(char_bytes) > max_bytes:
            break
        encoded += char_bytes
    return encoded.decode("utf-8").rstrip()

def _get_primary_artist(artist: str) -> str:
    separators = [" feat. ", " ft. ", " featuring ", " with ", " & ", ", ", " y "]
    for separator in separators:
        if separator in artist:
            return artist.split(separator)[0].strip()
    return artist.strip()

def _get_pure_album_dir(output_dir: Path, info: dict) -> Path:
    album = info.get("album", "Unknown Album")
    year = info.get("release_year")
    album = _sanitize_path_component(album)
    folder_name = f"{year} - {album}" if year else album
    folder_name = _sanitize_path_component(folder_name)
    return output_dir / "recent_download" / folder_name

def _get_output_path(
    output_dir: Path,
    info: dict,
    filename_template: str,
    organization_mode: str,
    use_primary_artist: bool = False,
) -> str:
    if organization_mode == "flat":
        return str(output_dir / f"{filename_template}.%(ext)s")

    if organization_mode == "pure_album":
        target_dir = _get_pure_album_dir(output_dir, info)
        track_number = info.get("track_number")
        if track_number:
            filename = f"{track_number:02d} - %(title)s.%(ext)s"
        else:
            filename = "%(title)s.%(ext)s"
        return str(target_dir / filename)

    # Album mode
    artist = info.get("artist") or info.get("uploader", "Unknown Artist")
    if use_primary_artist:
        artist = _get_primary_artist(artist)
    artist = _sanitize_path_component(artist)

    album = info.get("album")
    year = info.get("release_year")
    track_number = info.get("track_number")

    path_parts = [str(output_dir), artist]

    if album:
        album = _sanitize_path_component(album)
        album_folder = f"{year} - {album}" if year else album
        path_parts.append(album_folder)
        if track_number:
            filename = f"{track_number:02d} - %(title)s.%(ext)s"
        else:
            filename = "%(title)s.%(ext)s"
    else:
        filename = "%(title)s.%(ext)s"

    return str(Path(*path_parts) / filename)


def _get_ydl_opts(
    output_dir: Path,
    audio_format: str,
    filename_template: str,
    organization_mode: str,
    info: dict,
    progress_callback: callable = None,
    use_primary_artist: bool = False,
    cookie_file: str | None = None,
) -> dict:
    output_path = _get_output_path(
        output_dir, info, filename_template, organization_mode, use_primary_artist
    )

    opts = {
        "format": f"bestaudio[ext={audio_format}]/bestaudio[acodec*={audio_format}]/bestaudio/best",
        "outtmpl": output_path,
        "color": "never",
        "trim_file_name": MAX_FILENAME_BYTES,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": audio_format, "preferredquality": "0"},
            {"key": "FFmpegMetadata", "add_metadata": True},
            # ELIMINADO: {"key": "EmbedThumbnail"},  <-- Dejamos que nuestro código Python lo haga
        ],
        "writethumbnail": True, # Esto guarda la imagen original para que podamos leerla
        "quiet": True,
        "no_warnings": True,
        "retry_sleep_functions": {"http": lambda n: min(2**n, 30), "fragment": lambda n: min(2**n, 30)},
        "remote_components": ["ejs:github"],
    }

    if progress_callback:
        def progress_hook(d):
            if d["status"] == "downloading":
                downloaded = d.get("downloaded_bytes", 0)
                total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
                speed = d.get("speed", 0)
                eta = d.get("eta", 0)
                percent = (downloaded / total * 100) if total > 0 else 0

                if speed:
                    if speed > 1024 * 1024: speed_str = f"{speed / 1024 / 1024:.1f} MB/s"
                    elif speed > 1024: speed_str = f"{speed / 1024:.1f} KB/s"
                    else: speed_str = f"{speed:.0f} B/s"
                else: speed_str = "N/A"

                if eta:
                    if eta > 3600: eta_str = f"{int(eta) // 3600}h {(int(eta) % 3600) // 60}m"
                    elif eta > 60: eta_str = f"{int(eta) // 60}m {int(eta) % 60}s"
                    else: eta_str = f"{int(eta)}s"
                else: eta_str = "N/A"

                progress_callback({
                    "downloaded_bytes": downloaded, "total_bytes": total,
                    "percent": percent, "speed": speed_str, "eta": eta_str,
                })
        opts["progress_hooks"] = [progress_hook]

    return opts

def _compute_filename(info: dict, filename_template: str) -> str:
    with yt_dlp.YoutubeDL({"outtmpl": filename_template, "trim_file_name": MAX_FILENAME_BYTES}) as ydl:
        filename = ydl.prepare_filename(info)
    return yt_dlp.utils.sanitize_filename(filename)

def _file_exists(output_dir: Path, info: dict, audio_format: str, filename_template: str, organization_mode: str, use_primary_artist: bool = False) -> Path | None:
    if organization_mode in ("flat", "pure_album"):
        target_dir = output_dir if organization_mode == "flat" else _get_pure_album_dir(output_dir, info)
        expected_name = _compute_filename(info, filename_template)

        for ext in [audio_format, "opus", "mp3", "m4a", "flac"]:
            exact_path = target_dir / f"{expected_name}.{ext}"
            if exact_path.exists(): return exact_path
            matches = list(target_dir.glob(f"{expected_name[:50]}*.{ext}"))
            if matches: return matches[0]
        return None

    artist = info.get("artist") or info.get("uploader", "Unknown Artist")
    if use_primary_artist: artist = _get_primary_artist(artist)
    artist = _sanitize_path_component(artist)
    artist_dir = output_dir / artist
    if not artist_dir.exists(): return None

    title = info.get("title", "Unknown")
    for ext in [audio_format, "opus", "mp3", "m4a", "flac"]:
        for file_path in artist_dir.rglob(f"*{title}*.{ext}"):
            if file_path.is_file(): return file_path
    return None

def _apply_pure_album_tags(audio_path: Path, is_compilation: bool, album_artist: str = None) -> None:
    try:
        from mutagen import File
        audio = File(audio_path)
        if audio is None: return
        if is_compilation:
            audio["COMPILATION"] = ["1"]
            audio["ALBUMARTIST"] = ["Varios Artistas"]
        else:
            if "COMPILATION" in audio: del audio["COMPILATION"]
            target_album_artist = album_artist or (audio.get("artist", ["Unknown"])[0])
            audio["ALBUMARTIST"] = [target_album_artist]
        audio.save()
    except Exception as e:
        logger.warning("Failed to apply pure_album tags: %s", e)

def _cleanup_thumbnail(audio_path: Path):
    """Borra los archivos temporales de imagen que deja yt-dlp."""
    for ext in [".webp", ".jpg", ".jpeg", ".png"]:
        thumb = audio_path.with_suffix(ext)
        if thumb.exists():
            try:
                thumb.unlink()
                logger.debug("Deleted temporary thumbnail: %s", thumb.name)
            except OSError as e:
                logger.warning("Failed to delete thumbnail %s: %s", thumb.name, e)

def download(video_id: str, output_dir: Path, audio_format: str = "opus", filename_template: str = DEFAULT_FILENAME_TEMPLATE, fetch_lyrics: bool = True, progress_callback: callable = None, organization_mode: str = "flat", use_primary_artist: bool = False, cookie_file: str | None = None, artists: list[str] | None = None, apply_replaygain: bool = False) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    config = get_config()
    cooldown_hours = config.unavailable_cooldown_hours
    if is_on_cooldown(config.data_dir, video_id, cooldown_hours):
        logger.info("Skipping (unavailable cooldown): %s", video_id)
        raise UnavailableCooldownError(f"Video {video_id} is on unavailable cooldown.")

    url = f"https://music.youtube.com/watch?v={video_id}"
    try:
        ydl_opts_info = {"quiet": True, "no_warnings": True}
        info = extract_info_with_retry(ydl_opts=ydl_opts_info, url=url, download=False, cookie_file=cookie_file, config=config)
    except Exception as e:
        if is_unavailable_error(str(e)): record_unavailable(config.data_dir, video_id, str(e))
        raise

    title = info.get("title", "Unknown")
    artist = info.get("artist") or info.get("uploader", "Unknown")
    duration = info.get("duration", 0)

    existing = _file_exists(output_dir, info, audio_format, filename_template, organization_mode, use_primary_artist)
    if existing:
        logger.info("Skipping (exists): %s - %s", artist, title)
        return existing

    logger.info("Downloading: %s - %s", artist, title)

    try:
        ydl_opts = _get_ydl_opts(output_dir, audio_format, filename_template, organization_mode, info, progress_callback, use_primary_artist, cookie_file)
        extract_info_with_retry(ydl_opts=ydl_opts, url=url, download=True, cookie_file=cookie_file, config=config)
    except Exception as e:
        if is_unavailable_error(str(e)): record_unavailable(config.data_dir, video_id, str(e), title=title, artist=artist)
        raise

    audio_path = _find_downloaded_file(output_dir, info, audio_format, filename_template, organization_mode, use_primary_artist)

    if audio_path:
        if artists: write_multi_artist_tags(audio_path, artists)
        
        if organization_mode == "pure_album":
            is_comp = len(artists) > 1 if artists else False
            if not is_comp:
                artist_str = str(audio.get("artist", [""])[0]).lower()
                if any(sep in artist_str for sep in ["&", ",", " feat", " ft.", " featuring", " x ", ";", "/", " y "]): is_comp = True
            _apply_pure_album_tags(audio_path, is_compilation=is_comp)

        # APLICAMOS EL RECORTE DE PORTADA AQUÍ
        if info.get("thumbnail"):
            _embed_cover_art(audio_path, info.get("thumbnail"))
            _cleanup_thumbnail(audio_path) 

        if fetch_lyrics:
            lyrics = get_lyrics_for_video(video_id, title, artist, duration)
            if lyrics: save_lyrics(lyrics, audio_path)
        if apply_replaygain:
            from kikusan.replaygain import apply_replaygain as rsgain_apply
            rsgain_apply(audio_path, audio_format)
    return audio_path


def _extract_video_id_from_url(url: str) -> str | None:
    import re
    match = re.search(r"[?&]v=([a-zA-Z0-9_-]{11})", url)
    return match.group(1) if match else None

def download_url(url: str, output_dir: Path, audio_format: str = "opus", filename_template: str = DEFAULT_FILENAME_TEMPLATE, fetch_lyrics: bool = True, organization_mode: str = "flat", use_primary_artist: bool = False, cookie_file: str | None = None, apply_replaygain: bool = False) -> Path | list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        ydl_opts_info = {"quiet": True, "no_warnings": True, "ignoreerrors": "list=" in url}
        info = extract_info_with_retry(ydl_opts=ydl_opts_info, url=url, download=False, cookie_file=cookie_file, config=get_config())
    except Exception as e:
        video_id = _extract_video_id_from_url(url)
        if video_id and is_unavailable_error(str(e)): record_unavailable(get_config().data_dir, video_id, str(e))
        raise

    if info is None: raise RuntimeError(f"Could not extract info for {url}")

    if info.get("_type") == "playlist" or "entries" in info:
        return _download_playlist(info, output_dir, audio_format, filename_template, fetch_lyrics, organization_mode, use_primary_artist, cookie_file, apply_replaygain=apply_replaygain)

    return _download_single(url, info, output_dir, audio_format, filename_template, fetch_lyrics, organization_mode, use_primary_artist, cookie_file, apply_replaygain=apply_replaygain)


def _download_single(url: str, info: dict, output_dir: Path, audio_format: str, filename_template: str, fetch_lyrics: bool, organization_mode: str, use_primary_artist: bool = False, cookie_file: str | None = None, apply_replaygain: bool = False) -> Path:
    title = info.get("title", "Unknown")
    artist = info.get("artist") or info.get("uploader", "Unknown")
    duration = info.get("duration", 0)
    video_id = info.get("id")

    existing = _file_exists(output_dir, info, audio_format, filename_template, organization_mode, use_primary_artist)
    if existing:
        logger.info("Skipping (exists): %s - %s", artist, title)
        return existing

    logger.info("Downloading: %s - %s", artist, title)
    try:
        ydl_opts = _get_ydl_opts(output_dir, audio_format, filename_template, organization_mode, info, None, use_primary_artist, cookie_file)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl: ydl.download([url])
    except Exception as e:
        if video_id and is_unavailable_error(str(e)): record_unavailable(get_config().data_dir, video_id, str(e), title=title, artist=artist)
        raise

    audio_path = _find_downloaded_file(output_dir, info, audio_format, filename_template, organization_mode, use_primary_artist)
    if audio_path:
        if organization_mode == "pure_album":
            artist_str = str(artist).lower()
            is_comp = any(sep in artist_str for sep in ["&", ",", " feat", " ft.", " featuring", " x ", ";", "/", " y "])
            _apply_pure_album_tags(audio_path, is_compilation=is_comp)

        # APLICAMOS EL RECORTE DE PORTADA
        if info.get("thumbnail"):
            _embed_cover_art(audio_path, info.get("thumbnail"))
            _cleanup_thumbnail(audio_path) 

        if fetch_lyrics:
            lyrics = get_lyrics_for_video(video_id, title, artist, duration) if video_id else None
            if lyrics: save_lyrics(lyrics, audio_path)
        if apply_replaygain:
            from kikusan.replaygain import apply_replaygain as rsgain_apply
            rsgain_apply(audio_path, audio_format)
    return audio_path


def _download_playlist(info: dict, output_dir: Path, audio_format: str, filename_template: str, fetch_lyrics: bool, organization_mode: str, use_primary_artist: bool = False, cookie_file: str | None = None, apply_replaygain: bool = False) -> list[Path]:
    entries = info.get("entries", [])
    playlist_title = info.get("title", "Unknown Playlist")

    logger.info("Downloading playlist: %s (%d tracks)", playlist_title, len(entries))

    is_compilation_album = False
    if organization_mode == "pure_album":
        unique_artists = set()
        for entry in entries:
            if entry and entry.get("artist"): unique_artists.add(_get_primary_artist(entry.get("artist")))
        is_compilation_album = len(unique_artists) > 1

    downloaded = []
    skipped = 0
    config = get_config()
    cooldown_hours = config.unavailable_cooldown_hours

    for i, entry in enumerate(entries, 1):
        if entry is None: continue
        video_id = entry.get("id") or entry.get("url", "").split("=")[-1]
        title = entry.get("title", "Unknown")
        artist = entry.get("artist") or entry.get("uploader", "Unknown")
        duration = entry.get("duration", 0)

        if video_id and is_on_cooldown(config.data_dir, video_id, cooldown_hours):
            skipped += 1
            continue

        existing = _file_exists(output_dir, entry, audio_format, filename_template, organization_mode, use_primary_artist)
        if existing:
            downloaded.append(existing)
            skipped += 1
            continue

        logger.info("[%d/%d] Downloading: %s - %s", i, len(entries), artist, title)
        try:
            url = f"https://music.youtube.com/watch?v={video_id}"
            ydl_opts = _get_ydl_opts(output_dir, audio_format, filename_template, organization_mode, entry, None, use_primary_artist, cookie_file)
            extract_info_with_retry(ydl_opts=ydl_opts, url=url, download=True, cookie_file=cookie_file, config=config)

            audio_path = _find_downloaded_file(output_dir, entry, audio_format, filename_template, organization_mode, use_primary_artist)
            if audio_path:
                downloaded.append(audio_path)
                if organization_mode == "pure_album":
                    _apply_pure_album_tags(audio_path, is_compilation=is_compilation_album, album_artist=_get_primary_artist(artist))

                # APLICAMOS EL RECORTE DE PORTADA
                if entry.get("thumbnail"):
                    _embed_cover_art(audio_path, entry.get("thumbnail"))
                    _cleanup_thumbnail(audio_path) 

                if fetch_lyrics and video_id:
                    lyrics = get_lyrics_for_video(video_id, title, artist, duration)
                    if lyrics: save_lyrics(lyrics, audio_path)
                if apply_replaygain:
                    from kikusan.replaygain import apply_replaygain as rsgain_apply
                    rsgain_apply(audio_path, audio_format)

        except Exception as e:
            logger.warning("Failed to download %s: %s", title, e)
            if video_id and is_unavailable_error(str(e)): record_unavailable(config.data_dir, video_id, str(e), title=title, artist=artist)

    logger.info("Downloaded %d new tracks (%d skipped)", len(downloaded) - skipped, skipped)
    return downloaded


def _find_downloaded_file(output_dir: Path, info: dict, audio_format: str, filename_template: str, organization_mode: str, use_primary_artist: bool = False) -> Path | None:
    if organization_mode in ("flat", "pure_album"):
        target_dir = output_dir if organization_mode == "flat" else _get_pure_album_dir(output_dir, info)
        expected_name = _compute_filename(info, filename_template)
        for ext in [audio_format, "opus", "m4a", "webm"]:
            exact_path = target_dir / f"{expected_name}.{ext}"
            if exact_path.exists(): return exact_path
            matches = list(target_dir.glob(f"{expected_name[:50]}*.{ext}"))
            if matches: return matches[0]

        audio_extensions = ["opus", "mp3", "m4a", "flac", "webm"]
        all_audio = []
        if target_dir.exists():
            for ext in audio_extensions: all_audio.extend(target_dir.glob(f"*.{ext}"))
        if all_audio: return max(all_audio, key=lambda p: p.stat().st_mtime)
        return None

    artist = info.get("artist") or info.get("uploader", "Unknown Artist")
    if use_primary_artist: artist = _get_primary_artist(artist)
    artist = _sanitize_path_component(artist)
    artist_dir = output_dir / artist
    if not artist_dir.exists(): return None

    audio_extensions = ["opus", "mp3", "m4a", "flac", "webm"]
    all_audio = []
    for ext in audio_extensions: all_audio.extend(artist_dir.rglob(f"*.{ext}"))
    if all_audio: return max(all_audio, key=lambda p: p.stat().st_mtime)
    return None