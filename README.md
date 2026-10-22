# tgmedia

Download the media out of a Telegram group or channel, signed in as yourself.

`tgmedia fetch <chat>` walks the chat history, filters by kind, date, size, forum topic, or count, and writes each file into a per-chat, per-kind folder tree. A JSON ledger records what already landed, so a second run does no work and a crashed run recovers cleanly. Progress renders with rich while it works, and `--json` prints one machine-readable object instead.

## Why a user account

The Bot API cannot read group history. A bot only sees messages sent after it joined, and only what its admin rights allow it to fetch, so backfilling a group is impossible with a bot token. `tgmedia` signs in as you over MTProto with Telethon, which means it sees the same history your client sees.

## Install

Python 3.11 or newer is required.

```
python -m venv .venv
.venv/bin/pip install -e .
```

That install puts the `tgmedia` command in `.venv/bin`, which is on your PATH only while that environment is active. Do one of these two things to run the bare command from any directory.

- Activate the environment first. `source .venv/bin/activate`
- Link it once into a directory that is already on your PATH. `ln -sfn "$PWD/.venv/bin/tgmedia" ~/.local/bin/tgmedia`

Then confirm that `tgmedia --help` prints the usage text.

## Credentials

`tgmedia` needs an `api_id` and an `api_hash` for a Telegram application. Get both from [my.telegram.org](https://my.telegram.org) under API development tools. They identify the application, not your account, and every Telegram client on the machine can share one pair.

It reads them from the first source that supplies both.

| Source | How |
| --- | --- |
| Environment | `TG_API_ID` and `TG_API_HASH`. One variable without the other is ignored |
| Config file | `~/.config/tgmedia/config.toml` |
| Login prompt | `tgmedia login` asks for both when neither source has them, then offers to write the config file |

The config file is TOML, and the login prompt writes it with mode 0600.

```
api_id = 123456
api_hash = "0123456789abcdef0123456789abcdef"
```

Set `TGMEDIA_CONFIG_DIR` to keep the config and the session in a different directory, for example one per Telegram account.

## Sign in

`tgmedia login` signs in as a user account, not a bot, because only a user account can read group history. It asks for your phone number with the country code, then the code Telegram sends you in the app, then your two-factor password if you set one.

The session lands in `~/.config/tgmedia/session` as a `StringSession` with mode 0600, and every later command reuses it. That file is equivalent to your password. Keep it private, and revoke the entry in Telegram's active sessions list if it leaks.

`tgmedia chats` and `tgmedia fetch` exit with code 3 when the session is missing or expired, and print the command that fixes it.

## Commands

```
tgmedia login
tgmedia chats [--search Q]
tgmedia fetch <chat> [--message N] [options]
```

`tgmedia chats` lists your dialogs as a table of id, kind, username, and title.

A first pull goes in this order.

```
tgmedia login
tgmedia chats --search project
tgmedia fetch @project --dry-run
tgmedia fetch @project
```

To pull one message instead of the history, pass the message link Telegram gave you, or the id on its own.

```
tgmedia fetch https://t.me/project/45
tgmedia fetch @project --message 45
```

`tgmedia fetch --dry-run` prints the plan for that chat: per-kind item counts, the total size, how many items the ledger already has, and the destination root. It downloads nothing and creates nothing.

```
tgmedia fetch @groupname --dry-run
tgmedia fetch https://t.me/groupname --since 2024-01-01 --kind photo,video --dry-run
tgmedia fetch @groupname --out ./downloads --min-size 10k --max-size 2g --limit 200
```

`<chat>` accepts `@username`, a `t.me` link (`t.me/name`, `t.me/name/12`, `t.me/c/1234/56`), a numeric id (`-100...`), or a case-insensitive substring of a dialog title. An ambiguous substring prints every candidate and exits 2. A link that carries a message id downloads that one message, so a link pasted straight from Telegram needs no flag.

| flag | meaning |
| --- | --- |
| `--kind photo,video,...` | only these kinds. One of photo, sticker, gif, video, voice, audio, document |
| `--since`, `--until` | inclusive bounds. `YYYY-MM-DD` or a full ISO timestamp. A bare date on `--until` covers that whole UTC day |
| `--min-size`, `--max-size` | inclusive byte bounds. `500`, `10k`, `2g` |
| `--message N` | download that one message instead of walking the history |
| `--limit N` | stop after N new items |
| `--thread N` | only messages in that forum topic |
| `--out DIR` | destination root, default `./downloads` |
| `--concurrency N` | parallel downloads, default 4 |
| `--retry N` | retries per item on transient errors, default 3 |
| `--no-dedupe` | download a repost even when the file id was already captured |
| `--dry-run` | print the plan and exit |
| `--json` | print the plan or the summary as JSON on stdout, nothing else |
| `--quiet` | no progress bars and no summary |

Exit codes: `0` everything requested is on disk, `1` at least one item failed or Telegram refused the run, `2` usage error, unknown chat, ambiguous chat, or a message with nothing to download, `3` not logged in.

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
    photos/2024-05-01_101.jpg
    videos/2024-05-01_102.mov
    voice/2024-05-01_104.ogg
  .tgmedia/ledger.json
```

The chat folder is the username when the chat has one, else the title. Every character outside Unicode letters, digits, `_` and `-` becomes `-`, so a Thai or Arabic title stays readable and a title with spaces becomes `หล-บให-ท-น` or `Demo-Group`. The file name is `<UTC date>_<message id>_<document id>.<ext>`, with the document id omitted when Telegram reports none. The extension comes from the original file name when Telegram reports one, else from the kind default (photo `jpg`, sticker `webp`, gif `mp4`, video `mp4`, voice `ogg`, audio `mp3`, document `bin`). Video notes count as videos. Every name stays under 160 characters and cannot leave the destination root.

## When something goes wrong

The failures you are likely to hit exit with a code and one sentence instead of a traceback.

| What you see | What to do |
| --- | --- |
| `not logged in: the saved session is missing or expired` | Run `tgmedia login`. Exit 3 |
| `'project' matches 3 chats; pass one of:` | Pick from the table it prints, and pass the `@username` or the numeric id. Exit 2 |
| `nothing to download in @demo: message 2 is a photo, and --kind asked for video` | Drop the filter or drop `--message`. Exit 2 |
| `Telegram refused the run: Telegram asked to wait 120s before reading the history` | Run the same command after the wait. Telegram rate-limits large walks, and the ledger keeps what already landed. Exit 1 |

## Development

The test suite runs offline against an in-memory source, so it needs no account and no network.

```
.venv/bin/python -m pytest -q
```

`tests/fakes.py` implements the `TelegramSource` protocol. The Telethon adapter in `tgmedia/telegram.py` is the only module that talks to the network, so a change to filters, naming, the ledger, or the CLI can be proven without a login.

## Not included

- Bot tokens. Bots cannot read history, so the Bot API is not used at all.
- Byte-level resume of an interrupted file. The `.part` is discarded and the item is fetched again.
- Secret chats, stories, profile photos, polls, contacts, locations, and web link previews.
- Scheduled messages.
- Invite links (`t.me/+hash`). Use a username, a public link, an id, or a title substring.
- Several chats per run.
