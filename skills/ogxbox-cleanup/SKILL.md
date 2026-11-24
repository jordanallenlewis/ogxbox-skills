---
name: ogxbox-cleanup
description: 'Free space on a softmodded original Xbox by deleting folders or files the user names (an old game, its leftover saves or title data) over FTP. Always a dry-run plan first with every exact path, protected paths refused in code, then deletion only after the user approves that plan. Use when the user wants to remove a game, delete leftover saves or TDATA, or make room on the drive.'
---

# Clean up an original Xbox

## Setup this skill needs

- Python 3.9 or newer.
- `OGXBOX_HELPER`: the path to `lib/ogxbox_ftp.py` from the ogxbox-skills repository. If it is not set, ask the user where they cloned the repository.
- Connection settings in the environment: `OGXBOX_HOST` (the console address on the LAN), `OGXBOX_USER`, `OGXBOX_PASS`, and optionally `OGXBOX_PORT`. Many dashboards ship with user `xbox` and password `xbox`; the user may have changed them. Never write the password into a command, a file or the chat. If a variable is missing, ask the user to set it in their shell.
- The console is on, at its dashboard, with the FTP server enabled.

## Ground rules

- Back up first: run the ogxbox-backup skill before any cleanup.
- Delete only what the user names. Run the ogxbox-inventory skill to help them choose (it shows saves and title data whose game is no longer installed), but do not pick targets for them.
- Caches (the X, Y and Z partitions) are cleared with the dashboard's own option, never by deleting files.

## Never deleted (enforced in the helper, not just here)

- Anything on C:.
- `E:\Backups` and everything in it.
- nkpatcher settings (any folder named nkpatcher...).
- The dashboard: `E:\Dashboard`, `E:\Dashboards`, `E:\XBMC4Gamers`, and XBMC's P: and Q:.
- The `UDATA` and `TDATA` folders themselves. A single save or title folder inside them can be deleted only when the user names it, and only if no installed game has that title ID (the helper reads the title ID from every installed `default.xbe`, including games in category folders like `E:\games\Shooters\<Game>`, when planning and again when applying). If any installed game's title ID cannot be read, no save or title data is deleted at all.
- A folder that holds anything on this list (for example an nkpatcher folder inside a save) is refused whole.
- Drive roots.
- Anything the user adds with `--protect PATH` or in `OGXBOX_PROTECT` (paths separated by `;`).

## Steps

1. Make the plan for the paths the user named:

   ```
   python3 "$OGXBOX_HELPER" plan-cleanup "E:\games\Old Game" "/E/UDATA/4d530064" --out cleanup-plan.json
   ```

   The plan expands each folder into every file and folder it will delete, deepest first, with sizes and a total. Protected paths appear as `REFUSED` with the reason; nothing under them is planned.

2. Show the user the whole plan as printed, including every `REFUSED` and `NOTE` line. A note on a UDATA folder means it is a save folder: if it is the save the softmod was installed from or boots from, deleting it can stop the softmod loading. If the user is unsure, keep it.

3. Only after the user says yes to that exact plan:

   ```
   python3 "$OGXBOX_HELPER" apply cleanup-plan.json --approve <hash> --yes
   ```

   The helper deletes one item at a time and checks after each that it is gone. It stops at the first failure: report it and do not retry without asking.

4. Run the ogxbox-inventory skill again to show the result.
