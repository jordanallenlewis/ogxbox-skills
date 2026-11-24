---
name: ogxbox-title-id
description: 'Turn original Xbox title IDs (the 8-hex-digit folder names in E:\TDATA and E:\UDATA, like 4d530004) into game names, or read the title ID and name from a local default.xbe. Use when the user asks which game a TDATA or UDATA folder belongs to, or what a title ID means.'
---

# Original Xbox title IDs

Offline. No console connection needed.

## Setup this skill needs

- Python 3.9 or newer.
- `OGXBOX_HELPER`: the path to `lib/ogxbox_ftp.py` from the ogxbox-skills repository. If it is not set, ask the user where they cloned the repository.
- Optional: `OGXBOX_TITLE_IDS`, the path to the user's own CSV with the header `title_id,name`.

## Steps

- Look up one or more IDs:

  ```
  python3 "$OGXBOX_HELPER" title-id 4d530004 4d530064
  ```

- Read a local game's own title ID and name from its `default.xbe` (the most reliable source):

  ```
  python3 "$OGXBOX_HELPER" title-id --xbe "/path/to/Game/default.xbe"
  ```

- Use an extra list for this run only: `--csv /path/to/list.csv` (after `title-id`).

## Reading the answer

- `Name` comes from the game's `default.xbe`, from the user's CSV, or from the small bundled list (`title_ids.csv`, each row with its source). "unknown" means none of them has it.
- `Code` is the title ID decoded: two publisher letters and a game number, so `4d530064` is `MS-100`. It is shown even when the name is unknown.
- Do not guess a name for an unknown ID. Say it is unknown and give the code.

## The bundled list

`title_ids.csv` is a seed list on purpose; `SOURCE.md` explains why and lists the sources. Add a row only with a source whose licence allows redistribution.
