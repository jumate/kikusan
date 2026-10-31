# Fork notes

This is a fork of [dadav/kikusan](https://github.com/dadav/kikusan), which was archived by its owner on 2026-03-30. All credit for the project goes to the original author. The MIT license is kept as is.

The fork exists to keep Kikusan working with current YouTube behaviour. It is maintained on a best-effort basis for personal use.

## Changes compared to upstream 0.23.1

- **`Dockerfile`**: adds Deno (`COPY --from=denoland/deno:bin /deno /usr/local/bin/deno`). yt-dlp now needs a JavaScript runtime to solve YouTube's challenges. Without it, some formats are missing.
- **`pyproject.toml`**: `yt-dlp` becomes `yt-dlp[default]`, which pulls in `yt-dlp-ejs`, needed together with the JavaScript runtime.
- **`uv.lock`**: updated dependencies (yt-dlp 2026.10.26, yt-dlp-ejs 0.8.0).
- **`kikusan/download.py`**: removes the `extractor_args` override that pinned the YouTube clients to `android`, `web` and `web_safari`.

## Why

With the pinned clients, YouTube only returned the muxed 360p MP4 (format 18) for some videos. Kikusan then extracted its AAC track and re-encoded it to Opus, even though a native Opus stream (format 251) was available.

Symptoms of the problem:

- `Writing application: Lavc... libopus` in the resulting file (audio was re-encoded)
- Leftover MP4 container tags (`major_brand: mp42`, `handler_name: ISO Media file produced by Google`)

After the fix, yt-dlp picks the audio-only stream, which is copied without re-encoding (`Writing application: Lavf...`, no MP4 tags).

## Keeping it working

YouTube changes often, so yt-dlp needs regular updates:

    uv lock --upgrade-package yt-dlp

Then rebuild the Docker image without cache.

## Lyrics resilience (lyrics.py)

LRCLIB sometimes answers 503/429. Previously any error was treated as "no lyrics" and cached for 7 days. Now requests send a User-Agent, retry with backoff, pause for 60 seconds if the service stays down, and temporary failures are not cached as negative results.
