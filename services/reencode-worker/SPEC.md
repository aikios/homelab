# reencode-worker

GPU re-encode farm worker. Runs on the GPU host (vega) and turns media that the
TV can't direct-play into clean `mp4 / h264 / aac` so the Vizio webview plays it
without server-side transcoding.

## How it works
- Polls a job queue on the media host (`~/reencode_farm/queue` over ssh), claims
  one job at a time (atomic `ssh mv` into `claimed/`, so multiple workers are safe).
- `farm_core.process_job()`: ffprobe the source remotely; if already friendly, skip;
  else pull it over ssh and re-encode to h264 with a 3-tier ffmpeg fallback
  (NVENC → VAAPI GPU-decode → CPU-decode+VAAPI-encode). Extracts text subs as
  sidecars, uploads the result, **byte-verifies the remote size before removing the
  original**, then refreshes Jellyfin. Files the job under `done/` or `failed/`
  after writing a `result` block into it (what happened, sizes, encoder, time,
  whether the original was replaced); the media host DMs the owner from that
  (see reencode-submit "Discord notifications").

## Config (gitignored `.env`, see `.env.example`)
`FARM_SSH_HOST` (ssh target of the media host), `JELLYFIN_URL` + `JELLYFIN_API_KEY`
(the media host's Jellyfin). Optional: `REMOTE_FFPROBE`, `FARM_STAGE`,
`FARM_REMOTE_QUEUE`, `FARM_LOCK`, `FARM_LOG`.

## Layout
- `src/reencode_worker.py` — the daemon loop (claim → process → file result).
- `src/farm_core.py` — shared encode/verify/upload logic (imported by the worker).
- Jobs are submitted from the media host by the **reencode-submit** service.
- Stdlib only; runs on system `python3` (needs `ffmpeg` with NVENC/VAAPI + `ssh`/`scp`).

## Deploy (on the GPU host)
```sh
cp services/reencode-worker/.env.example services/reencode-worker/.env   # fill in
deploy/install.sh reencode-worker        # installs + enables reencode-worker.service
journalctl -u reencode-worker -f
```
Runtime log: `state/reencode_worker.log` (gitignored).

> Legacy note: `gpu_convert.py` (a standalone self-scanning converter) was the
> predecessor of this queue-based farm and is intentionally **not** imported.
