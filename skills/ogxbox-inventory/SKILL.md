---
name: ogxbox-inventory
description: 'Read-only inventory of a softmodded original Xbox over its dashboard FTP server. Lists the drives, the size of each top-level folder, installed games (folders with default.xbe under E:, F: or G: games), apps, saves (UDATA) and title data (TDATA) with their title IDs. Use when the user asks what is on their Xbox, how full it is, which games or saves it has, or before planning a backup, add-game or cleanup.'
---

# Original Xbox inventory

Read-only. This skill never changes anything on the console.

## Setup this skill needs

- Python 3.9 or newer.
- `OGXBOX_HELPER`: the path to `lib/ogxbox_ftp.py` from the ogxbox-skills repository. If it is not set, ask the user where they cloned the repository.
- Connection settings in the environment: `OGXBOX_HOST` (the console address on the LAN), `OGXBOX_USER`, `OGXBOX_PASS`, and optionally `OGXBOX_PORT`. Many dashboards ship with user `xbox` and password `xbox`; the user may have changed them. Never write the password into a command, a file or the chat. If a variable is missing, ask the user to set it in their shell.
- The console is on, at its dashboard, with the FTP server enabled.
- XBMC4Gamers keeps its settings per profile, and FTP can be on in one profile and off in another. The helper reports the active profile (or "unknown"). If FTP stops answering after a profile switch, check that FTP is enabled in that profile.

## If the console does not answer

If the helper exits with `Console FTP not reachable: start XBMC4Gamers and enable its FTP server, then re-run.` (exit code 3), stop. Tell the user that message, plus the hint below, and nothing else. Do not retry, loop, or try other addresses: the helper has already tried three times, and the server can disappear across a reboot or a profile switch.

What to tell the user alongside that message, if the dashboard is still on screen:

    FTP refused while the dashboard is on: set the screensaver to Dim, restart XBMC4Gamers

If a run ends with `INCOMPLETE` or exit code 4, the console stopped answering partway through. The result is partial: say so first, before anything else you report, and do not use it to decide a change (no add-game or cleanup from a partial list). Tell the user to start XBMC4Gamers, enable its FTP server and re-run.

## Steps

1. Check the connection and which drives the server exposes:

   ```
   python3 "$OGXBOX_HELPER" probe
   ```

   A drive that is not listed is not an error: many consoles have no F: or G:, and most dashboards do not expose the X, Y and Z cache partitions.

2. Run the inventory:

   ```
   python3 "$OGXBOX_HELPER" inventory
   ```

   Add `--json` before `inventory` to get machine-readable output. Add `--no-xbe` after it to skip reading each game's `default.xbe` header (faster; games then show no title ID).

3. Report to the user in plain words:
   - drives found and the biggest folders;
   - the active XBMC4Gamers profile, exactly as the helper prints it (`Active profile:`; "unknown" means it could not tell, so do not guess);
   - games, with the name and title ID read from each game's own `default.xbe`;
   - apps;
   - saves (UDATA) and title data (TDATA), and which of them belong to a game that is no longer installed;
   - every `warning:` line, unchanged. A warning about the loop guard or a folder that could not be opened means part of the tree was skipped: say which part.

## What the numbers mean

- Sizes are the sum of file sizes the server lists. They are not free space: FTP has no standard way to report it, and the dashboard FTP server usually cannot.
- C: and D: are listed as present but not walked.
- Title IDs with no name show as a code like `MS-100` (publisher letters and game number). The user can supply names with a CSV; see the ogxbox-title-id skill.

## Why the walk is careful

The XBMC FileZilla FTP server ignores a path given to `LIST` and lists the current folder instead. A walker that sends `LIST /E/games` loops forever. The helper always changes into a folder, checks where it landed, and lists with no argument. It also stops at a folder it has already seen and at a maximum depth (`--max-depth`, default 12).
