# tgmedia

Download the media out of a Telegram group or channel, signed in as yourself.

`tgmedia fetch <chat>` walks the chat history, filters by kind, date, size, forum topic, or count, and writes each file into a per-chat, per-kind folder tree. A JSON ledger records what already landed, so a second run does no work and a crashed run recovers cleanly. Progress renders with rich while it works, and `--json` prints one machine-readable object instead.

## Why a user account

The Bot API cannot read group history. A bot only sees messages sent after it joined, and only what its admin rights allow it to fetch, so backfilling a group is impossible with a bot token. `tgmedia` signs in as you over MTProto with Telethon, which means it sees the same history your client sees.

## Setup

1. Get an `api_id` and `api_hash` from [my.telegram.org](https://my.telegram.org) under API development tools. They identify the application, not your account.
2. Install the package.

```
python -m venv .venv
.venv/bin/pip install -e .
```

3. Sign in once. `tgmedia login` reads `TG_API_ID` and `TG_API_HASH` from the environment, else `~/.config/tgmedia/config.toml`, else it prompts for both and offers to write that config file with mode 0600.

```
tgmedia login
```

The session is stored as a `StringSession` at `~/.config/tgmedia/session` with mode 0600. That file is equivalent to your password, so keep it private.

## Commands

```
tgmedia login
tgmedia chats [--search Q]
tgmedia fetch <chat> [options]
```

`tgmedia chats` lists your dialogs as a table of id, kind, username, and title.

`tgmedia fetch --dry-run` prints the plan for that chat: per-kind item counts, the total size, how many items the ledger already has, and the destination root. It downloads nothing and creates nothing.

```
tgmedia fetch @groupname --dry-run
tgmedia fetch https://t.me/groupname --since 2024-01-01 --kind photo,video --dry-run
tgmedia fetch @groupname --out ./downloads --min-size 10k --max-size 2g --limit 200
```

`<chat>` accepts `@username`, a `t.me` link (`t.me/name`, `t.me/name/12`, `t.me/c/1234/56`), a numeric id (`-100...`), or a case-insensitive substring of a dialog title. An ambiguous substring prints every candidate and exits 2.

| flag | meaning |
| --- | --- |
| `--kind photo,video,...` | only these kinds. One of photo, sticker, gif, video, voice, audio, document |
| `--since`, `--until` | inclusive bounds. `YYYY-MM-DD` or a full ISO timestamp. A bare date on `--until` covers that whole UTC day |
| `--min-size`, `--max-size` | inclusive byte bounds. `500`, `10k`, `2g` |
| `--limit N` | stop after N new items |
| `--thread N` | only messages in that forum topic |
| `--out DIR` | destination root, default `./downloads` |
| `--concurrency N` | parallel downloads, default 4 |
| `--retry N` | retries per item on transient errors, default 3 |
| `--no-dedupe` | download a repost even when the file id was already captured |
| `--dry-run` | print the plan and exit |
| `--json` | print the plan or the summary as JSON on stdout, nothing else |
| `--quiet` | no progress bars and no summary |

Exit codes: `0` everything requested is on disk, `1` at least one item failed, `2` usage error, unknown chat, or ambiguous chat, `3` not logged in.

## Idempotency

Running `fetch` twice downloads nothing the second time. The ledger at `downloads/.tgmedia/ledger.json` is saved in batches of 20 finished items and flushed when the run ends, so a normal exit, a failed item, or Ctrl-C keeps every completed record. A hard kill can lose the last 19 records, which costs a re-download of at most 19 items. An item already in the ledger is skipped without touching the network.

A crash mid-file leaves only `<dest>.part`. That item is not in the ledger, so the next run fetches it again and the partial file is overwritten.

A `.part` whose final file already exists can only be a leftover from a crash between the rename and the ledger write. It is deleted on sight.

Reposts are skipped by default. Telegram keeps one document id for a file no matter how many times it is forwarded, so a second message carrying a known document id is skipped unless you pass `--no-dedupe`.

The ledger is a single writer, the orchestrator, and every save writes `ledger.json.tmp` in the same directory then renames it over the ledger. A corrupt or missing ledger is not fatal: it logs a warning and starts empty, which costs a re-download, never a wrong file.

## Output layout

```
downloads/
  demo/
    photos/2024-05-01_101.png
    videos/2024-05-01_102.mov
    voice/2024-05-01_104.ogg
  .tgmedia/ledger.json
```

The chat folder is the username when the chat has one, else the title, with every character outside letters, digits, `_` and `-` replaced by `-`. The file name is `<UTC date>_<message id>_<document id>.<ext>`. The extension comes from the original file name when Telegram reports one, else from the kind default (photo `jpg`, sticker `webp`, gif `mp4`, video `mp4`, voice `ogg`, audio `mp3`, document `bin`). Video notes count as videos. Every name stays under 160 characters and cannot leave the destination root.

## Not included

- Bot tokens. Bots cannot read history, so the Bot API is not used at all.
- Byte-level resume of an interrupted file. The `.part` is discarded and the item is fetched again.
- Secret chats, stories, profile photos, polls, contacts, locations, and web link previews.
- Scheduled messages.
- Invite links (`t.me/+hash`). Use a username, a public link, an id, or a title substring.
- Several chats per run.
