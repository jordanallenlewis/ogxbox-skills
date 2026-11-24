---
name: ogxbox-backup
description: 'Copy a softmodded original Xbox''s irreplaceable files (the EEPROM backup in E:\Backups\EEPROM, game saves in E:\UDATA, and the dashboard profile) to a private local folder over FTP. Use before ANY change to the console, when the user asks to back up their Xbox, their saves or their EEPROM, or when another ogxbox skill asks for a backup first.'
---

# Original Xbox backup

Reads from the console, writes only on the user's computer.

## Setup this skill needs

- Python 3.9 or newer.
- `OGXBOX_HELPER`: the path to `lib/ogxbox_ftp.py` from the ogxbox-skills repository. If it is not set, ask the user where they cloned the repository.
- Connection settings in the environment: `OGXBOX_HOST` (the console address on the LAN), `OGXBOX_USER`, `OGXBOX_PASS`, and optionally `OGXBOX_PORT`. Many dashboards ship with user `xbox` and password `xbox`; the user may have changed them. Never write the password into a command, a file or the chat. If a variable is missing, ask the user to set it in their shell.
- The console is on, at its dashboard, with the FTP server enabled.

## Before you start, tell the user

- `eeprom.bin` and `xbox info.txt` hold the console's hard drive key and password. Anyone with them can unlock that drive.
- The backup folder must stay off cloud sync (Dropbox, OneDrive, iCloud, Google Drive) and out of git. Ask where to put it if they have not said; a folder in their home directory is a good default.

## Steps

1. Run the backup into the folder the user chose:

   ```
   python3 "$OGXBOX_HELPER" backup ~/xbox-backups
   ```

   It creates `~/xbox-backups/ogxbox-backup-<date>-<time>/` with:
   - `eeprom/` from `/E/Backups/EEPROM`
   - `saves/` from `/E/UDATA`
   - `dashboard-profile/` from `/P` (XBMC's profile: dashboard settings)
   - a `.gitignore` that ignores everything in the folder.

   Folders are created owner-only (0700) and files owner-only (0600). To copy another folder too, add `--source label=/E/path`.

2. Read the result. Every file shows its size and `Verified: yes` when the copied size matches the size on the console. Any `ERROR` line or `Verified: NO` means that backup is incomplete: say so plainly and do not go on to a change.

3. If a source is `skipped ... not found`, tell the user. A console with no `E:\Backups\EEPROM` may never have had its EEPROM backed up by the softmod installer; they should fix that before changing anything.

## Never

- Never print, cat, hexdump, hash or summarise the contents of `eeprom.bin`, `xbox info.txt` or any file in the backup. Report names and sizes only.
- Never copy the backup into a git repository, a shared folder or a chat.
- Never upload anything to the console in this skill.
