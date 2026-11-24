# Where the title IDs come from

`title_ids.csv` is a small seed list on purpose.

I looked for a complete original Xbox title-ID list with a licence that allows it to be copied into an MIT project. The public lists I found carry no licence, so this repository does not bundle them. A list of names and numbers is mostly facts, but without a clear licence it stays out.

What ships instead:

- **Two rows**, each a fact quoted with its source: the worked examples on the Xbox Dev Wiki's XBE page (revision 7427, <https://xboxdevwiki.net/index.php?title=Xbe&oldid=7427>, section "Title ID"). The source is in the third column of every row.
- **The game's own name, read from its `default.xbe`.** Every XBE carries a certificate with the title ID and the title name (layout: <https://xboxdevwiki.net/Xbe>, and Caustik's "Xbe File Format", <http://www.caustik.com/cxbx/download/xbe.htm>). Inventory reads it for every installed game, so installed games are named without any list.
- **Your own list.** Point `OGXBOX_TITLE_IDS` at a CSV with the header `title_id,name` (a `source` column is optional). Your rows are read after the bundled ones and win on a clash. Use a list you have the right to use.
- **A decoded code for anything unknown.** A title ID is two ASCII publisher letters and a game number (Xbox Dev Wiki, same section), so `4d530064` shows as `MS-100` even when no name is known.

No box art, icons or other copyrighted assets are included.

To add rows here, include a source URL for each one, and only use a source whose licence allows redistribution (MIT, CC0, CC BY or public domain).
