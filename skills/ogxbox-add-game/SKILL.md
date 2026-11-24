---
name: ogxbox-add-game
description: 'Copy an extracted original Xbox game folder (from the user''s own disc, with default.xbe at its top) to E:\games, F:\games or G:\games on a softmodded console over FTP. Dry run first with FATX name and size checks and the exact list of remote paths, then upload only after the user approves that exact plan. Use when the user wants to put a game they own onto their Xbox hard drive.'
---

# Add a game to an original Xbox

## Setup this skill needs

- Python 3.9 or newer.
- `OGXBOX_HELPER`: the path to `lib/ogxbox_ftp.py` from the ogxbox-skills repository. If it is not set, ask the user where they cloned the repository.
- Connection settings in the environment: `OGXBOX_HOST` (the console address on the LAN), `OGXBOX_USER`, `OGXBOX_PASS`, and optionally `OGXBOX_PORT`. Many dashboards ship with user `xbox` and password `xbox`; the user may have changed them. Never write the password into a command, a file or the chat. If a variable is missing, ask the user to set it in their shell.
- The console is on, at its dashboard, with the FTP server enabled.

## Ground rules

- Only games the user owns, from their own disc. Do not help find, download or convert games from anywhere else.
- Back up first: if the user has no backup from today, run the ogxbox-backup skill before step 3.
- Nothing is written until the user has seen the exact paths and approved them.

## Steps

1. Check the local folder against FATX limits:

   ```
   python3 "$OGXBOX_HELPER" check-fatx "/path/to/Game Folder"
   ```

   FATX allows names of at most 42 characters and files of at most 4,294,967,295 bytes (4 GB). The helper also accepts only letters, digits, space and `` ! # $ % & ' ( ) - . @ [ ] ^ _ ` { } ~ `` in names. Every problem is listed. Help the user rename files or choose a shorter folder name (`--name`); do not rename game files on your own, because a game may look for them by name.

2. Make the plan (a dry run; nothing is written):

   ```
   python3 "$OGXBOX_HELPER" plan-add-game "/path/to/Game Folder" --drive E --out game-plan.json
   ```

   Use `--title "Short Name"` to choose the folder name on the console. The plan lists every folder and file it will create, with sizes, a total, and the plan hash.

3. Show the user the plan output as printed: the target folder, the total size, the free space line and every `PROBLEM` line. Free space: the helper asks the server with the `AVBL` command. Most dashboard FTP servers do not support it, and then the plan says free space is unknown. In that case ask the user to check free space on the console's own dashboard, and only go on if they confirm it is enough.

4. Only after the user says yes to that exact plan, apply it with the hash it printed:

   ```
   python3 "$OGXBOX_HELPER" apply game-plan.json --approve <hash> --yes
   ```

   Add `--free-space-ok` only when the user has confirmed free space themselves. The helper uploads one file at a time in binary mode and checks each file's size on the console after it is written. It stops at the first failure: report the failing path and do not retry without asking.

5. Tell the user how to see the game: the dashboard has to rescan its games folder. In XBMC4Gamers use the dashboard's own option to scan or refresh the games list (its name differs between versions); a restart of the dashboard also works.

## Refused by the helper, whatever the user asks

- Writing to C:, D:, P:, Q: or the X, Y and Z caches. Games go on E:, F: or G:.
- Paths with `..`, `.` or empty parts.
- Overwriting a game folder that already exists.
- Applying a plan that has problems, was edited after it was printed, or without `--yes` and the matching hash.
