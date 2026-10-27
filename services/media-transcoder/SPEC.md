# media-transcoder

Re-encodes library files the TV can't direct-play into clean `mp4 / h264 / aac`,
so the Vizio webview plays them without server-side transcoding. The work is a
job queue: the media host (blacklodge) finds and queues files, a GPU host (vega)
claims and encodes them, and the owner gets a Discord DM at both ends.
(Formerly `reencode-submit` + `reencode-worker`; merged 2026-10-26.)

## Units (per host: `deploy/install.sh media-transcoder <cmd>...`)
| unit                                     | host  | what                                                  |
|------------------------------------------|-------|-------------------------------------------------------|
| `media-transcoder-submit.{service,timer}`| media | `submit --scan` every 5 min                            |
| `media-transcoder-notify.{service,path}` | media | DM as soon as a worker files a job in `done/`/`failed/`|
| `media-transcoder-worker.service`        | GPU   | resident worker: claim → encode → file result          |

In the inventory: `media-transcoder:submit,notify` on the media host,
`media-transcoder:worker` on the GPU host.

## Queue protocol
Jobs are json files under `~/reencode_farm/{queue,claimed,done,failed}` on the
media host (`MEDIA_TRANSCODER_QUEUE_DIR`). A worker claims one with an atomic
`ssh mv` into `claimed/` (so several workers are safe), processes it, writes a
`result` block into the job (`status`, `detail`, `mode`, `out_bytes`, `secs`,
`subs`, `replaced`, `error`, `worker`, `finished`) and only then `mv`s it into
`done/` or `failed/`; that move is what the notify path unit watches.

## Modules (`src/media_transcoder/`)
- `submit.py` — CLI `media-transcoder-submit`:
  ```sh
  media-transcoder-submit              # show queue/claimed/done/failed
  media-transcoder-submit --scan       # queue every non-direct-playable Jellyfin item
  media-transcoder-submit <file> ...   # submit explicit paths
  ```
  `--scan` is idempotent (skips queued/claimed, and done paths unchanged since
  their job was filed: a file rewritten later, e.g. remuxed, is checked again;
  failed jobs are retried) and
  skips files missing, zero-size, or modified within `MEDIA_TRANSCODER_STABLE_SECS`
  (still importing). It auto-loads the service `.env`. The *arr apps run in Docker
  and can't reach the host queue without bind-mount plumbing, hence a 5-min poll
  rather than import hooks.
- `notify.py` — Discord DMs via homelab-core (shared bot token):
  **queued** (one batched DM per scan/submit: container, codecs, size) and
  **re-encoded / failed / skipped** (from the job's `result` block). Reported ids
  live in `~/reencode_farm/notified.json`; the first run seeds it with the backlog;
  an id is only marked once Discord accepts the DM, so an outage retries; a flock
  serialises the path unit and the scan's backstop call. Best-effort: never
  breaks submitting or filing.
- `worker.py` — the daemon loop (claim → `core.process_job` → file result).
- `core.py` — ffprobe the source remotely; skip if already friendly; else pull it
  over ssh and encode with a 3-tier ffmpeg fallback (NVENC → VAAPI GPU-decode →
  CPU-decode + VAAPI-encode); extract text subs as sidecars (one read for all
  tracks; ONE per language, the primary track via `homelab_core.subtitles`; a
  language that already has a sidecar, e.g. media-concierge's styled
  `<stem>.en.default.ass`, is left alone); upload;
  **byte-verify the remote size before removing the original**; refresh Jellyfin.

## Config (gitignored `.env`, see `.env.example`)
Media host: `JELLYFIN_URL` (local) + `JELLYFIN_API_KEY`; optional
`MEDIA_TRANSCODER_QUEUE_DIR`, `MEDIA_TRANSCODER_STABLE_SECS`.
GPU host: `MEDIA_TRANSCODER_SSH_HOST`, `JELLYFIN_URL` (the media host's) +
`JELLYFIN_API_KEY`; optional `MEDIA_TRANSCODER_{REMOTE_FFPROBE,STAGE,REMOTE_QUEUE,LOCK,LOG}`.
The path unit hard-codes the default queue dir; change it there too if you
override `MEDIA_TRANSCODER_QUEUE_DIR`. Needs `ffmpeg` with NVENC/VAAPI and
`ssh`/`scp` on the GPU host.

## Ops
```sh
journalctl -u media-transcoder-worker -f        # GPU host; also state/worker.log
uv run --no-sync python -m unittest discover -s services/media-transcoder/tests
```
