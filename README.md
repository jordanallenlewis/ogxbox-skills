# ogxbox-skills

Claude Code skills for looking after a softmodded original Xbox over its dashboard's FTP server: see what is on it, back up what can't be replaced, copy a game from your own disc onto the hard drive, and clear out what you no longer need.

Five skills share one small Python helper (`lib/ogxbox_ftp.py`, standard library only). It only reads unless you approve a plan that lists every path it will write or delete.

| Skill | What it does | Changes the console? |
|---|---|---|
| `ogxbox-inventory` | Drives, folder sizes, games, apps, saves (UDATA), title data (TDATA), with title IDs | No |
| `ogxbox-backup` | Copies the EEPROM backup, saves and dashboard profile to a private local folder | No |
| `ogxbox-add-game` | Checks an extracted game folder against FATX limits, plans the upload, uploads after you approve | Yes, after approval |
| `ogxbox-cleanup` | Plans deleting the folders you name, refuses protected ones, deletes after you approve | Yes, after approval |
| `ogxbox-title-id` | Turns a title ID like `4d530004` into a name, or reads it from a `default.xbe` | No (offline) |

## What it is not

- Not a softmod installer and not an exploit. Your console must already be softmodded with a dashboard that runs an FTP server.
- Not a source of games. It copies games you extracted from your own discs. It does not download, share or convert anything.
- It ships no ROMs, BIOS images, keys, box art or icons.

## Quick start

1. Clone this repository and note where `lib/ogxbox_ftp.py` is.
2. Copy the skills into Claude Code, either for every project or for one:

   ```
   cp -r skills/* ~/.claude/skills/
   # or: cp -r skills/* /path/to/project/.claude/skills/
   ```

3. Set the connection in your shell. Use your console's LAN address. `xbox` / `xbox` is a common dashboard default; use your own if you changed it.

   ```
   export OGXBOX_HELPER=/path/to/ogxbox-skills/lib/ogxbox_ftp.py
   export OGXBOX_HOST=<your console's LAN address>
   export OGXBOX_USER=xbox
   export OGXBOX_PASS=xbox
   # optional: export OGXBOX_PORT=21
   ```

4. Start Claude Code and ask "what's on my Xbox?". The inventory skill runs. You can also run the helper yourself:

   ```
   python3 "$OGXBOX_HELPER" inventory
   ```

The helper reads settings from these variables or from `--host`, `--user`, `--password` and `--port`. Nothing is stored in the repository, and the password is never printed.

## Safety rules

- **Read-only by default.** `probe`, `ls`, `inventory`, `backup` and `title-id` never write to the console.
- **Plan, approve, apply.** `plan-add-game` and `plan-cleanup` write a plan file listing every remote path and size, and print its hash. `apply` runs only with `--yes` and that exact hash, so nothing is written that you have not seen. A plan that was edited, or has problems, is refused.
- **Never C:.** Writes to C: are refused in code, and so are writes to D:, P:, Q: and the X, Y and Z caches. Games go on E:, F: or G:.
- **Never-delete list in code.** C:, `E:\Backups`, nkpatcher settings, the dashboard folders, the UDATA and TDATA folders themselves, and the saves and title data of any game that is still installed (title IDs read from each installed `default.xbe`, at plan time and again at apply time; if one cannot be read, no saves or title data are deleted). Caches are cleared with the dashboard's own option, never by deleting files.
- **Safe paths only.** `..`, `.`, empty path parts and paths that do not start at a drive are rejected. Anything written or deleted must have a FATX-valid name, so wildcards and trailing dots never reach the server.
- **One operation at a time, verified.** Only one apply runs per console (a lock file in `~/.cache/ogxbox`). Each upload or delete runs alone, then the helper lists the folder again and checks the size, or checks that the item is gone. It stops at the first failure. Uploads never overwrite an existing file, and never write inside a protected folder.
- **Back up first.** Run `ogxbox-backup` before any change. `eeprom.bin` and `xbox info.txt` hold your hard drive key: the backup folder is owner-only (0700, files 0600), comes with a `.gitignore`, and the contents are never printed. Keep it off cloud sync and out of git.
- **FTP is cleartext.** Use it on your home network only. Never port-forward the console to the internet.

## How it walks the drive

The XBMC FileZilla FTP server (banner `XBMC:FileZilla 1.5.6`) ignores a path given to `LIST` and lists the current folder instead. A tool that walks the drive with `LIST /C/foo` never moves, and recurses forever (`/C/C/C/...`). This helper always changes into the folder (`CWD`), checks where it landed (`PWD`), then sends `LIST` with no argument. It never uses `MLSD`. It visits each real folder once and stops at a maximum depth.

## Profiles and a console that goes quiet

XBMC4Gamers keeps its settings per profile. `P:` is the active profile, and FTP can be on in one profile and off in another. `inventory` and `backup` report the active profile, or "unknown" when the helper cannot tell (it never guesses). If FTP stops answering after you switch profile, check that FTP is enabled in that profile.

The FTP server can also disappear across a reboot or a profile switch. The helper does not wait for it: it tries to connect three times (10 seconds each), then exits with code 3 and `Console FTP not reachable: start XBMC4Gamers and enable its FTP server, then re-run.` If the server vanishes in the middle of a walk, the result is marked `incomplete: true`, printed as `INCOMPLETE`, and the exit code is 4. A partial result is never reported as a success.

Drives appear as folders at the FTP root: `/C`, `/D` (DVD), `/E`, `/F` and `/G` (if partitioned), `/P` and `/Q` (XBMC profile and home). The helper checks which exist; a missing drive is not an error.

## Troubleshooting

**FTP is refused, or stops answering, while the XBMC4Gamers dashboard is still on screen.** Cause: the Planestate 3D screensaver. On one 64 MB console the built-in FTP server (`XBMC:FileZilla 1.5.6`) went from up, to unresponsive, to refused 18 minutes into idle with that screensaver running. Fix: in XBMC4Gamers settings, set the screensaver to Dim with a long timeout. With that setting the same console stayed up for 41+ minutes idle. Recovery: restart XBMC4Gamers. Once the FTP server has crashed it does not come back by itself. This is one console and one observation, not a tested rule.

## Title IDs

Saves and title data live in folders named after a game's title ID, like `E:\UDATA\4d530004`. Names come from three places, in this order:

1. The game's own `default.xbe`, whose certificate holds its title ID and name. Inventory reads it for every installed game.
2. Your own CSV, if you set `OGXBOX_TITLE_IDS` (header `title_id,name`).
3. A small bundled seed list, `skills/ogxbox-title-id/title_ids.csv`, where every row cites its source.

The bundled list is small because no complete list with a redistribution licence was found; `skills/ogxbox-title-id/SOURCE.md` explains. An unknown ID is still decoded into publisher letters and a game number, for example `4d530064` is `MS-100`.

## Tested on

Designed against XBMC4Gamers installed with Rocky5's Xbox Softmodding Tool, on a v1.6 console with the stock 8 GB HDD. Tests run against a mocked FTP server.

This helper has not yet been run against a real console. A separate read-only walk that uses the same method (change into a folder, then `LIST` with no argument) has run against one: it confirmed the server quirk above and that `/C`, `/E`, `/P` and `/Q` are exposed. Which folders sit inside `E:` (`Backups`, `Dashboard` and so on) and how the active profile is named are not confirmed yet. The mock copies the server behaviour described above, but treat the first runs on your own console with care: start with `inventory` and `backup`, and read every plan before you apply it. Reports of what works and what doesn't are welcome as issues.

## Development

```
python3 -m unittest discover -s tests -v
python3 scripts/check_public.py
```

Python 3.9 or newer, no dependencies. `tests/fake_xbox.py` is a stand-in for `ftplib.FTP` with the real server's quirks. CI runs both commands on Python 3.9 and 3.12.

## Licence and trademarks

MIT, see `LICENSE`.

"Xbox" is a trademark of Microsoft. This project is not affiliated with or endorsed by Microsoft.
