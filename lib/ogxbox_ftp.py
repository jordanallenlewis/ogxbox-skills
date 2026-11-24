#!/usr/bin/env python3
"""Manage a softmodded original Xbox over its dashboard FTP server.

One file, Python 3.9+ standard library only (ftplib). Read-only by default.
Anything that writes goes through plan -> approval -> apply.

Connection settings come from the environment or from flags, never from
this file:

    OGXBOX_HOST   console address on your LAN (required)
    OGXBOX_USER   FTP user (the common dashboard default is "xbox")
    OGXBOX_PASS   FTP password (the common dashboard default is "xbox")
    OGXBOX_PORT   FTP port (optional, default 21)

Run `python3 ogxbox_ftp.py --help` for the subcommands.

Server quirk this file is built around: the XBMC FileZilla FTP server
(banner "XBMC:FileZilla 1.5.6") ignores a path given to LIST and lists the
current directory instead. Walking a tree with `LIST /C/foo` therefore
never moves and recurses forever (/C/C/C/...). This helper always changes
directory first (CWD), checks where it landed (PWD), and then sends LIST
with no argument. It never sends MLSD.
"""

import argparse
import contextlib
import ftplib
import hashlib
import json
import os
import re
import struct
import sys
import time

VERSION = "0.1.0"

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

# Drive letters the dashboard FTP server may expose as top-level folders.
# C system, D DVD, E data, F/G extra partitions, P/Q XBMC profile and home,
# X/Y/Z game caches (most dashboards do not expose them).
DRIVE_LETTERS = ["C", "D", "E", "F", "G", "P", "Q", "X", "Y", "Z"]
CACHE_DRIVES = {"X", "Y", "Z"}
GAME_DRIVES = ["E", "F", "G"]
WRITABLE_DRIVES = {"E", "F", "G"}

# FATX limits. A directory entry holds the name in a 42-byte ASCII field and
# the file size in a 4-byte field:
#   https://web.archive.org/web/20100617022009/http://www.xbox-linux.org/wiki/Differences_between_Xbox_FATX_and_MS-DOS_FAT
#   https://hackmd.io/DXo0p8VnQLy3XQ7YR1Ny_w?view  ("The FATX filesystem", 2021)
FATX_MAX_NAME = 42
FATX_MAX_FILE = 4294967295  # 2**32 - 1, the largest value a 4-byte size holds
# Characters accepted in a FATX name. Letters, digits, space and the
# punctuation below. Neither source above lists the character set, so this
# list is deliberately conservative: anything outside it is reported and the
# upload is refused.
FATX_EXTRA_CHARS = set(" !#$%&'()-.@[]^_`{}~")

DEFAULT_TIMEOUT = 30
DEFAULT_MAX_DEPTH = 16
XBE_HEAD_BYTES = 64 * 1024
PLAN_FORMAT = "ogxbox-plan-v1"

# Files whose contents must never be printed, hashed to the screen, or left
# readable by other users.
SENSITIVE_NAMES = {"eeprom.bin", "xbox info.txt"}

# Paths that cleanup never deletes, whatever the user asks. Lower case, FTP
# form. A path is protected when it is one of these, sits inside one, or is a
# parent of one.
NEVER_DELETE = [
    "/C",                 # system drive: never written at all
    "/E/Backups",         # EEPROM and softmod backups
    "/E/Dashboard",       # common dashboard install folders
    "/E/Dashboards",
    "/E/XBMC4Gamers",
    "/P",                 # XBMC profile (dashboard settings)
    "/Q",                 # XBMC home (the running dashboard)
]
# These folders themselves are never deleted, but a single save or title
# folder inside them may be, when the user names it and its game is not
# installed.
NEVER_DELETE_FOLDER_ONLY = ["/E/UDATA", "/E/TDATA", "/F/UDATA", "/F/TDATA", "/G/UDATA", "/G/TDATA"]
# Any path component that starts with one of these is protected too
# (nkpatcher settings live in a folder named after it).
NEVER_DELETE_COMPONENT_PREFIXES = ["nkpatcher"]


class OgxError(Exception):
    """A problem the user should see. Never carries a password."""


class SafetyError(OgxError):
    """A refused operation: unsafe path, protected path, no approval."""


# --------------------------------------------------------------------------
# Remote paths
# --------------------------------------------------------------------------

def to_remote(path):
    """Turn `E:\\games\\Foo`, `E:/games/Foo` or `/E/games/Foo` into
    `/E/games/Foo`. Rejects `..`, `.`, empty components and anything that
    does not start at a drive."""
    if not isinstance(path, str) or not path.strip():
        raise SafetyError("empty remote path")
    p = path.strip().replace("\\", "/")
    m = re.match(r"^([A-Za-z]):(/.*)?$", p)
    if m:
        p = "/" + m.group(1).upper() + (m.group(2) or "")
    if not p.startswith("/"):
        raise SafetyError("remote path must start at a drive, like /E/games: %r" % path)
    if p == "/":
        return "/"
    body = p[1:]
    if body.endswith("/"):
        body = body[:-1]
    parts = body.split("/")
    for part in parts:
        if part == "":
            raise SafetyError("empty path component in %r" % path)
        if part in (".", ".."):
            raise SafetyError("'.' and '..' are not allowed in %r" % path)
        if "\x00" in part or "\r" in part or "\n" in part:
            raise SafetyError("control characters are not allowed in %r" % path)
    if len(parts[0]) != 1 or not parts[0].isalpha():
        raise SafetyError("first component must be a drive letter, like /E: %r" % path)
    parts[0] = parts[0].upper()
    return "/" + "/".join(parts)


def remote_join(base, name):
    if "/" in name or "\\" in name or name in ("", ".", ".."):
        raise SafetyError("bad name %r" % name)
    return to_remote(base.rstrip("/") + "/" + name)


def remote_parent(path):
    path = to_remote(path)
    if path.count("/") <= 1:
        return "/"
    return path.rsplit("/", 1)[0]


def remote_name(path):
    return to_remote(path).rsplit("/", 1)[-1]


def drive_of(path):
    path = to_remote(path)
    return path[1:2] if path != "/" else ""


def same_path(a, b):
    """FATX is case-insensitive; compare that way."""
    return a.rstrip("/").lower() == b.rstrip("/").lower()


def is_within(path, root):
    p, r = path.lower().rstrip("/"), root.lower().rstrip("/")
    return p == r or p.startswith(r + "/")


def guard_write(path):
    """Every write primitive calls this. Hard refusals, in code."""
    path = to_remote(path)
    if path == "/":
        raise SafetyError("refusing to write at the FTP root")
    drive = drive_of(path)
    if drive == "C":
        raise SafetyError("refusing to write to C: (%s). C: holds the system files." % path)
    if drive not in WRITABLE_DRIVES:
        raise SafetyError("refusing to write to %s: (%s). Only E:, F: and G: are writable here."
                          % (drive, path))
    if path.count("/") == 1:
        raise SafetyError("refusing to change a drive root (%s)" % path)
    return path


def protected_reason(path, extra_protected=(), protected_titles=()):
    """Why cleanup must not delete `path`, or None.

    `protected_titles` are title IDs (lower-case hex) of games still
    installed; their TDATA and UDATA folders are kept."""
    path = to_remote(path)
    lower = path.lower()
    if path == "/" or path.count("/") == 1:
        return "drive roots are never deleted"
    if drive_of(path) in CACHE_DRIVES:
        return "cache partitions are cleared with the dashboard's own option, never by deleting files"
    for root in list(NEVER_DELETE) + [to_remote(p) for p in extra_protected]:
        if is_within(path, root):
            return "%s is on the never-delete list" % root
        if is_within(root, path):
            return "%s contains %s, which is on the never-delete list" % (path, root)
    for root in NEVER_DELETE_FOLDER_ONLY:
        if is_within(root, path):
            return "%s is kept; name a single save or title folder inside it instead" % root
    for comp in lower.split("/")[1:]:
        for prefix in NEVER_DELETE_COMPONENT_PREFIXES:
            if comp.startswith(prefix):
                return "nkpatcher settings are never deleted"
    m = re.match(r"^/[a-z]/(udata|tdata)/([0-9a-f]{8})(/|$)", lower)
    if m and m.group(2) in protected_titles:
        return "title %s is still installed; its %s folder is kept" % (m.group(2), m.group(1).upper())
    return None


def env_protected(environ=None):
    """Extra never-delete paths from $OGXBOX_PROTECT, separated by ';'."""
    env = os.environ if environ is None else environ
    return [p.strip() for p in env.get("OGXBOX_PROTECT", "").split(";") if p.strip()]


def guard_delete(path, extra_protected=(), protected_titles=()):
    path = guard_write(path)
    reason = protected_reason(path, extra_protected, protected_titles)
    if reason:
        raise SafetyError("refusing to delete %s: %s" % (path, reason))
    return path


# --------------------------------------------------------------------------
# LIST parsing
# --------------------------------------------------------------------------

_LIST_RE = re.compile(
    r"^(?P<perm>[\-dlcbps][rwxsStT\-]{9})\s+"
    r"(?P<links>\d+)\s+"
    r"(?P<owner>\S+)\s+"
    r"(?P<group>\S+)\s+"
    r"(?P<size>\d+)\s+"
    r"(?P<month>[A-Za-z]{3})\s+"
    r"(?P<day>\d{1,2})\s+"
    r"(?P<timeyear>\d{1,2}:\d{2}|\d{4})\s"
    r"(?P<name>.+)$"
)


def parse_list_line(line):
    """Parse one Unix-style LIST line. Returns a dict, or None for lines
    that are not entries ("total 12", blanks, ".", "..")."""
    line = line.rstrip("\r\n")
    m = _LIST_RE.match(line)
    if not m:
        return None
    name = m.group("name")
    if name in (".", ".."):
        return None
    perm = m.group("perm")
    if perm[0] == "l" and " -> " in name:
        name = name.split(" -> ", 1)[0]
    return {
        "name": name,
        "is_dir": perm[0] == "d",
        "size": int(m.group("size")),
        "mtime": "%s %s %s" % (m.group("month"), m.group("day"), m.group("timeyear")),
    }


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

class Settings(object):
    def __init__(self, host, user, password, port=21, timeout=DEFAULT_TIMEOUT, passive=True):
        self.host = host
        self.user = user
        self._password = password
        self.port = int(port)
        self.timeout = timeout
        self.passive = passive

    @property
    def password(self):
        return self._password

    def __repr__(self):
        return "Settings(host=%r, user=%r, password=<hidden>, port=%r, passive=%r)" % (
            self.host, self.user, self.port, self.passive)

    __str__ = __repr__

    @classmethod
    def from_args(cls, args, environ=None):
        env = os.environ if environ is None else environ
        host = getattr(args, "host", None) or env.get("OGXBOX_HOST")
        user = getattr(args, "user", None) or env.get("OGXBOX_USER")
        password = getattr(args, "password", None) or env.get("OGXBOX_PASS")
        port = getattr(args, "port", None) or env.get("OGXBOX_PORT") or 21
        missing = [n for n, v in (("OGXBOX_HOST", host), ("OGXBOX_USER", user),
                                  ("OGXBOX_PASS", password)) if not v]
        if missing:
            raise OgxError("missing connection settings: %s (set them in the environment "
                           "or pass --host/--user/--password)" % ", ".join(missing))
        try:
            port = int(port)
        except ValueError:
            raise OgxError("OGXBOX_PORT must be a number")
        return cls(host, user, password, port=port,
                   timeout=getattr(args, "timeout", None) or DEFAULT_TIMEOUT,
                   passive=not getattr(args, "active", False))


# --------------------------------------------------------------------------
# The client
# --------------------------------------------------------------------------

class XboxFTP(object):
    """A thin, careful wrapper around ftplib.FTP.

    `ftp_factory` lets tests hand in a fake server; it is called with
    `timeout=` and must return an object with ftplib.FTP's methods."""

    def __init__(self, settings, ftp_factory=None, log=None):
        self.settings = settings
        self._factory = ftp_factory or (lambda timeout: ftplib.FTP(timeout=timeout))
        self.ftp = None
        self.banner = ""
        self.warnings = []
        self._log = log or (lambda msg: None)
        self._busy = False

    # connection -----------------------------------------------------------

    def connect(self):
        s = self.settings
        self.ftp = self._factory(timeout=s.timeout)
        try:
            self.banner = self.ftp.connect(s.host, s.port, timeout=s.timeout) or ""
            self.ftp.login(s.user, s.password)
        except ftplib.error_perm:
            raise OgxError("login refused: check OGXBOX_USER and OGXBOX_PASS")
        except (OSError, EOFError, ftplib.Error) as exc:
            raise OgxError("could not connect to the console FTP server: %s" % type(exc).__name__)
        self.ftp.set_pasv(s.passive)
        return self

    def close(self):
        if self.ftp is not None:
            try:
                self.ftp.quit()
            except Exception:
                try:
                    self.ftp.close()
                except Exception:
                    pass
            self.ftp = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

    def warn(self, msg):
        self.warnings.append(msg)
        self._log("warning: " + msg)

    @contextlib.contextmanager
    def _one_op(self):
        """One write at a time: a second write while one runs is refused."""
        if self._busy:
            raise SafetyError("another operation is still running; one at a time")
        self._busy = True
        try:
            yield
        finally:
            self._busy = False

    # navigation -----------------------------------------------------------

    def cd(self, path):
        """CWD into `path` and confirm with PWD. Returns the PWD path.
        Raises ftplib.error_perm if the folder does not exist."""
        path = to_remote(path)
        self.ftp.cwd(path)
        here = self.ftp.pwd()
        try:
            here_n = to_remote(here) if here not in ("", "/") else "/"
        except SafetyError:
            here_n = here
        if not same_path(here_n, path):
            raise OgxError("CWD %s landed in %s" % (path, here))
        return here_n

    def list_dir(self, path):
        """Entries of one folder. CWD first, then LIST with no argument."""
        here = self.cd(path)
        lines = []
        self.ftp.retrlines("LIST", lines.append)
        entries = []
        for line in lines:
            e = parse_list_line(line)
            if e is not None:
                entries.append(e)
        return here, entries

    def exists_dir(self, path):
        try:
            self.cd(path)
            return True
        except ftplib.error_perm:
            return False

    def stat_entry(self, path):
        """The LIST entry for `path` in its parent, or None."""
        parent, name = remote_parent(path), remote_name(path)
        try:
            _, entries = self.list_dir(parent)
        except ftplib.error_perm:
            return None
        for e in entries:
            if e["name"].lower() == name.lower():
                return e
        return None

    def probe_drives(self, letters=None):
        found = []
        for letter in letters or DRIVE_LETTERS:
            try:
                self.cd("/" + letter)
                found.append(letter)
            except ftplib.error_perm:
                pass  # a drive the server does not expose is not an error
            except OgxError as exc:
                self.warn(str(exc))
        return found

    def walk(self, root, max_depth=DEFAULT_MAX_DEPTH):
        """Yield (path, entries) for `root` and every folder below it.

        Loop guard: each real folder (by PWD) is visited once, depth is
        capped, and a CWD that lands somewhere unexpected stops that branch
        with a warning."""
        root = to_remote(root)
        visited = set()
        stack = [(root, 0)]
        while stack:
            path, depth = stack.pop()
            try:
                here, entries = self.list_dir(path)
            except ftplib.error_perm as exc:
                self.warn("cannot open %s (%s)" % (path, str(exc)[:40]))
                continue
            except OgxError as exc:
                self.warn("%s; skipping that branch" % exc)
                continue
            key = here.lower()
            if key in visited:
                self.warn("already visited %s; skipping (loop guard)" % here)
                continue
            visited.add(key)
            yield here, entries
            if depth >= max_depth:
                if any(e["is_dir"] for e in entries):
                    self.warn("max depth %d reached at %s; not going deeper" % (max_depth, here))
                continue
            for e in sorted((e for e in entries if e["is_dir"]), key=lambda e: e["name"].lower(),
                            reverse=True):
                try:
                    stack.append((remote_join(here, e["name"]), depth + 1))
                except SafetyError as exc:
                    self.warn("skipping odd entry in %s: %s" % (here, exc))

    def tree_size(self, root, max_depth=DEFAULT_MAX_DEPTH):
        total, files = 0, 0
        for _, entries in self.walk(root, max_depth):
            for e in entries:
                if not e["is_dir"]:
                    total += e["size"]
                    files += 1
        return total, files

    # reads ----------------------------------------------------------------

    def _binary(self):
        self.ftp.voidcmd("TYPE I")

    def download(self, remote, write):
        """Download one file in binary mode, passing chunks to `write`."""
        remote = to_remote(remote)
        self.cd(remote_parent(remote))
        self._binary()
        self.ftp.retrbinary("RETR " + remote_name(remote), write)

    def read_head(self, remote, nbytes=XBE_HEAD_BYTES):
        """The first `nbytes` of a remote file, without fetching all of it."""
        remote = to_remote(remote)
        self.cd(remote_parent(remote))
        self._binary()
        conn = self.ftp.transfercmd("RETR " + remote_name(remote))
        buf = b""
        try:
            while len(buf) < nbytes:
                chunk = conn.recv(min(8192, nbytes - len(buf)))
                if not chunk:
                    break
                buf += chunk
        finally:
            conn.close()
        try:
            self.ftp.voidresp()  # 226 if complete, 426 if we stopped early
        except (ftplib.error_temp, ftplib.error_perm, ftplib.error_reply):
            pass
        return buf

    def free_space(self, path):
        """Free bytes on the drive holding `path`, or None if the server
        cannot say. Tries the AVBL command; FTP has no standard way."""
        try:
            resp = self.ftp.sendcmd("AVBL " + to_remote(path))
        except (ftplib.Error, OSError, EOFError):
            return None
        m = re.match(r"^2\d\d\s+(\d+)", resp or "")
        return int(m.group(1)) if m else None

    # writes (every one guarded, one at a time, verified after) -------------

    def mkdir(self, remote):
        remote = guard_write(remote)
        with self._one_op():
            self.cd(remote_parent(remote))
            self.ftp.mkd(remote_name(remote))
            if not self.exists_dir(remote):
                raise OgxError("verify failed: %s was not created" % remote)

    def upload(self, local, remote):
        remote = guard_write(remote)
        size = os.path.getsize(local)
        with self._one_op():
            self.cd(remote_parent(remote))
            self._binary()
            with open(local, "rb") as fh:
                self.ftp.storbinary("STOR " + remote_name(remote), fh)
            entry = self.stat_entry(remote)
            if entry is None or entry["is_dir"] or entry["size"] != size:
                raise OgxError("verify failed: %s is %s on the console, expected %d bytes"
                               % (remote, "missing" if entry is None else entry["size"], size))

    def delete_file(self, remote, extra_protected=(), protected_titles=()):
        remote = guard_delete(remote, extra_protected, protected_titles)
        with self._one_op():
            self.cd(remote_parent(remote))
            self.ftp.delete(remote_name(remote))
            if self.stat_entry(remote) is not None:
                raise OgxError("verify failed: %s is still there" % remote)

    def remove_dir(self, remote, extra_protected=(), protected_titles=()):
        remote = guard_delete(remote, extra_protected, protected_titles)
        with self._one_op():
            self.cd(remote_parent(remote))
            self.ftp.rmd(remote_name(remote))
            if self.stat_entry(remote) is not None:
                raise OgxError("verify failed: %s is still there" % remote)


# --------------------------------------------------------------------------
# Title IDs and XBE certificates
# --------------------------------------------------------------------------

# XBE layout, from https://xboxdevwiki.net/Xbe and Caustik's "Xbe File Format"
# (http://www.caustik.com/cxbx/download/xbe.htm):
#   0x0000  u32  magic "XBEH"
#   0x0104  u32  base address (usually 0x00010000)
#   0x0118  u32  certificate address (virtual; file offset = addr - base)
# Certificate:
#   0x0000  u32  size of certificate
#   0x0004  u32  time/date
#   0x0008  u32  title ID
#   0x000C  80 bytes  title name, UTF-16LE, zero padded (40 characters)

def parse_xbe(data):
    """Return {"title_id": "4d530004", "title_name": "..."} from the start
    of a default.xbe, or raise OgxError."""
    if len(data) < 0x11C or data[0:4] != b"XBEH":
        raise OgxError("not an XBE file (no XBEH header)")
    base, = struct.unpack_from("<I", data, 0x104)
    cert_addr, = struct.unpack_from("<I", data, 0x118)
    off = cert_addr - base
    if off < 0 or off + 0x0C + 80 > len(data):
        raise OgxError("XBE certificate is outside the bytes read")
    title_id, = struct.unpack_from("<I", data, off + 0x08)
    raw = data[off + 0x0C: off + 0x0C + 80]
    name = raw.decode("utf-16-le", errors="replace").split("\x00", 1)[0].strip()
    return {"title_id": "%08x" % title_id, "title_name": name}


def normalize_title_id(value):
    v = str(value).strip().lower()
    if v.startswith("0x"):
        v = v[2:]
    if not re.match(r"^[0-9a-f]{8}$", v):
        raise OgxError("a title ID is 8 hex digits, like 4d530004: %r" % value)
    return v


def describe_title_id(tid):
    """Decode the publisher letters and game number, e.g. MS-004."""
    tid = normalize_title_id(tid)
    hi = bytes.fromhex(tid[:4])
    num = int(tid[4:], 16)
    if all(0x20 < b < 0x7f for b in hi):
        return "%s-%03d" % (hi.decode("ascii"), num)
    return None


def default_title_csv():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "..", "skills", "ogxbox-title-id", "title_ids.csv")


def load_title_ids(paths=None):
    """Read title-ID CSV files (columns: title_id,name[,source]). Later
    files win, so a user CSV can correct the bundled list."""
    import csv
    if paths is None:
        paths = [default_title_csv()]
        user = os.environ.get("OGXBOX_TITLE_IDS")
        if user:
            paths.append(user)
    table = {}
    for p in paths:
        if not p or not os.path.exists(p):
            continue
        with open(p, newline="", encoding="utf-8") as fh:
            rows = (r for r in fh if r.strip() and not r.lstrip().startswith("#"))
            for row in csv.DictReader(rows):
                try:
                    tid = normalize_title_id(row.get("title_id", ""))
                except OgxError:
                    continue
                name = (row.get("name") or "").strip()
                if name:
                    table[tid] = name
    return table


def lookup_title(tid, table):
    tid = normalize_title_id(tid)
    return {"title_id": tid, "name": table.get(tid), "code": describe_title_id(tid),
            "known": tid in table}


# --------------------------------------------------------------------------
# FATX checks (local folder)
# --------------------------------------------------------------------------

def fatx_name_problems(name):
    problems = []
    if len(name) > FATX_MAX_NAME:
        problems.append("name is %d characters; FATX allows %d" % (len(name), FATX_MAX_NAME))
    bad = sorted(set(c for c in name if not (c.isascii() and c.isalnum()) and c not in FATX_EXTRA_CHARS))
    if bad:
        problems.append("characters FATX does not accept: %s" % " ".join(repr(c) for c in bad))
    if name != name.strip() or name.endswith("."):
        problems.append("name starts or ends with a space, or ends with a dot")
    return problems


def check_fatx_tree(local_root, target_name=None):
    """Every FATX violation under `local_root`. Nothing is uploaded until
    this list is empty."""
    violations = []
    top = target_name or os.path.basename(os.path.abspath(local_root))
    for p in fatx_name_problems(top):
        violations.append({"path": top, "problem": p})
    for dirpath, dirnames, filenames in os.walk(local_root):
        dirnames.sort()
        rel = os.path.relpath(dirpath, local_root)
        for name in sorted(dirnames) + sorted(filenames):
            relname = name if rel == "." else os.path.join(rel, name).replace(os.sep, "/")
            for p in fatx_name_problems(name):
                violations.append({"path": relname, "problem": p})
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                violations.append({"path": relname, "problem": "symbolic links are not uploaded"})
            elif name in filenames and os.path.getsize(full) > FATX_MAX_FILE:
                violations.append({"path": relname, "problem": "file is %d bytes; FATX allows %d (4 GB)"
                                   % (os.path.getsize(full), FATX_MAX_FILE)})
    return violations


# --------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------

def _find_child(entries, name, is_dir=None):
    for e in entries:
        if e["name"].lower() == name.lower() and (is_dir is None or e["is_dir"] == is_dir):
            return e
    return None


def installed_games(client, drives=None, read_xbe=True, max_depth=DEFAULT_MAX_DEPTH):
    """Folders with a default.xbe directly under <drive>/games."""
    games = []
    for d in drives or GAME_DRIVES:
        root = "/%s/games" % d
        try:
            _, entries = client.list_dir(root)
        except ftplib.error_perm:
            continue
        for e in sorted((e for e in entries if e["is_dir"]), key=lambda e: e["name"].lower()):
            path = remote_join(root, e["name"])
            try:
                _, inner = client.list_dir(path)
            except (ftplib.error_perm, OgxError) as exc:
                client.warn("cannot open %s: %s" % (path, exc))
                continue
            xbe = _find_child(inner, "default.xbe", is_dir=False)
            if xbe is None:
                continue
            game = {"path": path, "folder": e["name"], "title_id": None, "title_name": None}
            if read_xbe:
                try:
                    info = parse_xbe(client.read_head(remote_join(path, xbe["name"])))
                    game.update(info)
                except (OgxError, ftplib.Error, OSError) as exc:
                    client.warn("could not read the title ID of %s: %s" % (path, exc))
            games.append(game)
    return games


def inventory(client, max_depth=DEFAULT_MAX_DEPTH, read_xbe=True, titles=None):
    titles = load_title_ids() if titles is None else titles
    drives = client.probe_drives()
    report = {"banner": client.banner.strip(), "drives": drives, "folders": [],
              "games": [], "apps": [], "saves": [], "tdata": [], "caches": [], "notes": []}
    all_sizes = {}
    for d in drives:
        if d in ("C", "D"):
            continue  # system and DVD: listed, not walked
        sizes = {}
        for path, entries in client.walk("/" + d, max_depth):
            sizes[path] = sum(e["size"] for e in entries if not e["is_dir"])
        # roll file sizes up to each top-level folder
        tops = {}
        for path, size in sizes.items():
            parts = path.split("/")
            top = "/".join(parts[:3]) if len(parts) > 2 else path
            tops[top] = tops.get(top, 0) + size
        for top in sorted(tops):
            report["folders"].append({"path": top, "bytes": tops[top]})
        if d in CACHE_DRIVES:
            report["caches"].append({"drive": d, "bytes": sum(sizes.values())})
        all_sizes.update(sizes)

    def size_under(root):
        return sum(v for k, v in all_sizes.items() if is_within(k, root))

    missing_caches = [c for c in sorted(CACHE_DRIVES) if c not in drives]
    if missing_caches:
        report["notes"].append("cache partitions %s are not exposed over FTP; clear caches from the "
                               "dashboard's own settings" % ", ".join(missing_caches))
    games = installed_games(client, [d for d in GAME_DRIVES if d in drives], read_xbe, max_depth)
    for g in games:
        g["bytes"] = size_under(g["path"])
        if g.get("title_id") and not g.get("title_name"):
            g["title_name"] = titles.get(g["title_id"])
    report["games"] = games
    for d in [d for d in GAME_DRIVES if d in drives]:
        for folder in ("apps", "Apps"):
            try:
                _, entries = client.list_dir("/%s/%s" % (d, folder))
            except ftplib.error_perm:
                continue
            for e in entries:
                if e["is_dir"]:
                    p = remote_join("/%s/%s" % (d, folder), e["name"])
                    report["apps"].append({"path": p, "bytes": size_under(p)})
            break
    installed = {g["title_id"] for g in games if g.get("title_id")}
    for kind in ("UDATA", "TDATA"):
        try:
            _, entries = client.list_dir("/E/" + kind)
        except ftplib.error_perm:
            continue
        for e in entries:
            if not e["is_dir"]:
                continue
            p = remote_join("/E/" + kind, e["name"])
            item = {"path": p, "bytes": size_under(p), "title_id": None,
                    "name": None, "installed": False}
            try:
                tid = normalize_title_id(e["name"])
                item.update(title_id=tid, name=titles.get(tid) or describe_title_id(tid),
                            installed=tid in installed)
            except OgxError:
                pass
            report["saves" if kind == "UDATA" else "tdata"].append(item)
    report["warnings"] = list(client.warnings)
    return report


# --------------------------------------------------------------------------
# Backup
# --------------------------------------------------------------------------

DEFAULT_BACKUP_SOURCES = [
    ("eeprom", "/E/Backups/EEPROM"),
    ("saves", "/E/UDATA"),
    ("dashboard-profile", "/P"),
]

CLOUD_HINTS = ("dropbox", "onedrive", "icloud", "google drive", "googledrive", "box sync", "mega")


def _inside_git(path):
    p = os.path.abspath(path)
    while True:
        if os.path.exists(os.path.join(p, ".git")):
            return p
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


def _private_dir(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


def _private_file(path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(path, 0o600)
    return os.fdopen(fd, "wb")


def is_sensitive(name):
    return name.lower() in SENSITIVE_NAMES


def backup(client, dest, sources=None, max_depth=DEFAULT_MAX_DEPTH):
    """Copy the console's irreplaceable files to `dest`. Contents are never
    printed. The folder is 0700 and every file 0600."""
    dest = os.path.abspath(dest)
    report = {"dest": dest, "copied": [], "skipped": [], "warnings": [], "errors": []}
    lower = dest.lower()
    if any(h in lower for h in CLOUD_HINTS):
        report["warnings"].append("this folder looks cloud-synced; keep EEPROM backups off cloud sync")
    repo = _inside_git(dest)
    if repo:
        report["warnings"].append("this folder is inside a git repository (%s); keep it out of git" % repo)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    _private_dir(dest)
    root = os.path.join(dest, "ogxbox-backup-" + stamp)
    n = 1
    while os.path.exists(root):  # never mix two backups in one folder
        n += 1
        root = os.path.join(dest, "ogxbox-backup-%s-%d" % (stamp, n))
    _private_dir(root)
    with _private_file(os.path.join(root, ".gitignore")) as fh:
        fh.write(b"# EEPROM and console info files hold your hard drive key. Never commit them.\n*\n")
    for label, src in sources or DEFAULT_BACKUP_SOURCES:
        src = to_remote(src)
        if not client.exists_dir(src):
            report["skipped"].append({"source": src, "why": "not found on the console"})
            continue
        for path, entries in client.walk(src, max_depth):
            rel = path[len(src):].lstrip("/")
            local_dir = os.path.join(root, label, *rel.split("/")) if rel else os.path.join(root, label)
            _private_dir(local_dir)
            for e in entries:
                if e["is_dir"]:
                    continue
                remote = remote_join(path, e["name"])
                local = os.path.join(local_dir, e["name"])
                try:
                    with _private_file(local) as fh:
                        client.download(remote, fh.write)
                except (ftplib.Error, OSError, OgxError) as exc:
                    report["errors"].append({"remote": remote, "error": type(exc).__name__})
                    continue
                got = os.path.getsize(local)
                item = {"remote": remote, "local": local, "bytes": got,
                        "verified": got == e["size"], "sensitive": is_sensitive(e["name"])}
                if got != e["size"]:
                    report["errors"].append({"remote": remote,
                                             "error": "size mismatch: %d on console, %d copied" % (e["size"], got)})
                report["copied"].append(item)
    for d, dirs, files in os.walk(root):
        os.chmod(d, 0o700)
        for f in files:
            os.chmod(os.path.join(d, f), 0o600)
    report["root"] = root
    report["warnings"].extend(client.warnings)
    return report


# --------------------------------------------------------------------------
# Plans: plan -> approval -> apply
# --------------------------------------------------------------------------

def plan_hash(plan):
    body = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def write_plan(plan, path):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return plan_hash(plan)


def read_plan(path):
    with open(path, encoding="utf-8") as fh:
        plan = json.load(fh)
    if plan.get("format") != PLAN_FORMAT:
        raise SafetyError("not an ogxbox plan file")
    return plan


def plan_add_game(client, local_dir, drive="E", title=None):
    local_dir = os.path.abspath(local_dir)
    if not os.path.isdir(local_dir):
        raise OgxError("not a folder: %s" % local_dir)
    drive = drive.upper()
    if drive not in GAME_DRIVES:
        raise SafetyError("games go on E:, F: or G:")
    title = title or os.path.basename(local_dir)
    entries = os.listdir(local_dir)
    if not any(n.lower() == "default.xbe" for n in entries):
        raise OgxError("no default.xbe at the top of %s; point at the extracted game folder" % local_dir)
    violations = check_fatx_tree(local_dir, title)
    target = remote_join("/%s/games" % drive, title) if not fatx_name_problems(title) else None
    ops, total = [], 0
    if target:
        guard_write(target)
        if not client.exists_dir("/%s/games" % drive):
            ops.append({"op": "mkdir", "remote": "/%s/games" % drive})
        ops.append({"op": "mkdir", "remote": target})
        for dirpath, dirnames, filenames in os.walk(local_dir):
            dirnames.sort()
            rel = os.path.relpath(dirpath, local_dir)
            rbase = target if rel == "." else target + "/" + rel.replace(os.sep, "/")
            for d in dirnames:
                ops.append({"op": "mkdir", "remote": rbase + "/" + d})
            for f in sorted(filenames):
                full = os.path.join(dirpath, f)
                size = os.path.getsize(full)
                total += size
                ops.append({"op": "upload", "local": full, "remote": rbase + "/" + f, "bytes": size})
        if client.exists_dir(target):
            violations.append({"path": target, "problem": "already exists on the console; "
                               "remove or rename it first"})
    free = client.free_space("/%s" % drive)
    plan = {"format": PLAN_FORMAT, "kind": "add-game", "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "target": target, "total_bytes": total, "free_bytes": free,
            "free_space_known": free is not None, "violations": violations, "ops": ops,
            "protected_titles": []}
    if free is not None and free < total:
        plan["violations"].append({"path": "/%s" % drive, "problem": "needs %d bytes, %d free" % (total, free)})
    return plan


def plan_cleanup(client, targets, extra_protected=(), max_depth=DEFAULT_MAX_DEPTH):
    """Expand each named target into the exact files and folders to delete
    (deepest first). Protected paths are refused here and again at apply."""
    games = installed_games(client, read_xbe=True)
    protected_titles = sorted({g["title_id"] for g in games if g.get("title_id")})
    refused, ops, total = [], [], 0
    for t in targets:
        try:
            t = guard_write(t)
        except SafetyError as exc:
            refused.append({"path": t, "why": str(exc)})
            continue
        why = protected_reason(t, extra_protected, protected_titles)
        if why:
            refused.append({"path": t, "why": why})
            continue
        entry = client.stat_entry(t)
        if entry is None:
            refused.append({"path": t, "why": "not found on the console"})
            continue
        if not entry["is_dir"]:
            ops.append({"op": "delete", "remote": t, "bytes": entry["size"]})
            total += entry["size"]
            continue
        dirs = []
        warned = len(client.warnings)
        for path, entries in client.walk(t, max_depth):
            dirs.append(path)
            for e in entries:
                if not e["is_dir"]:
                    p = remote_join(path, e["name"])
                    ops.append({"op": "delete", "remote": p, "bytes": e["size"]})
                    total += e["size"]
        if len(client.warnings) > warned:
            refused.append({"path": t, "why": "the walk was incomplete (%s); nothing under it is planned"
                            % client.warnings[-1]})
            ops = [o for o in ops if not is_within(o["remote"], t)]
            continue
        for d in sorted(dirs, key=lambda p: p.count("/"), reverse=True):
            ops.append({"op": "rmdir", "remote": d})
    notes = []
    for o in ops:
        if o["op"] == "rmdir" and re.match(r"^/[a-z]/udata/[^/]+$", o["remote"].lower()):
            notes.append("%s is a save folder. If it is the save your softmod was installed or "
                         "boots from, deleting it can stop the softmod loading. Keep it if unsure."
                         % o["remote"])
    return {"format": PLAN_FORMAT, "kind": "cleanup", "notes": notes, "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "total_bytes": total, "refused": refused, "violations": [], "ops": ops,
            "protected_titles": protected_titles, "extra_protected": list(extra_protected)}


def check_plan(plan):
    """Re-check every operation in a plan. Raises SafetyError."""
    if plan.get("violations"):
        raise SafetyError("the plan has %d problem(s); fix them and plan again" % len(plan["violations"]))
    titles = plan.get("protected_titles") or []
    extra = plan.get("extra_protected") or []
    for op in plan.get("ops", []):
        kind = op.get("op")
        if kind in ("mkdir", "upload"):
            guard_write(op["remote"])
        elif kind in ("delete", "rmdir"):
            guard_delete(op["remote"], extra, titles)
        else:
            raise SafetyError("unknown operation %r" % kind)
        if kind == "upload":
            if not os.path.isfile(op["local"]):
                raise SafetyError("local file is gone: %s" % op["local"])
            if os.path.getsize(op["local"]) != op["bytes"]:
                raise SafetyError("local file changed since planning: %s" % op["local"])


def apply_plan(client, plan, approved_hash, yes=False, free_space_ok=False, log=None):
    """Run a plan the user has seen. Needs --yes and the plan's hash."""
    log = log or (lambda m: None)
    if not yes:
        raise SafetyError("apply needs --yes (after you have read the plan)")
    if not approved_hash or approved_hash != plan_hash(plan):
        raise SafetyError("the approval hash does not match this plan; read the plan and pass "
                          "--approve <hash> exactly as printed")
    check_plan(plan)
    if plan.get("kind") == "add-game" and not plan.get("free_space_known") and not free_space_ok:
        raise SafetyError("the server cannot report free space; check it on the console and pass "
                          "--free-space-ok")
    done = []
    titles = list(plan.get("protected_titles") or [])
    extra = list(plan.get("extra_protected") or []) + env_protected()
    if plan.get("kind") == "cleanup":
        # Do not trust the plan file alone: ask the console again which games are installed.
        titles += [g["title_id"] for g in installed_games(client) if g.get("title_id")]
    for i, op in enumerate(plan["ops"], 1):
        kind, remote = op["op"], op["remote"]
        log("[%d/%d] %s %s" % (i, len(plan["ops"]), kind, remote))
        if kind == "mkdir":
            if client.exists_dir(remote):
                done.append({"op": kind, "remote": remote, "note": "already there"})
                continue
            client.mkdir(remote)
        elif kind == "upload":
            client.upload(op["local"], remote)
        elif kind == "delete":
            client.delete_file(remote, extra, titles)
        elif kind == "rmdir":
            client.remove_dir(remote, extra, titles)
        done.append({"op": kind, "remote": remote, "verified": True})
    return {"applied": len(done), "ops": done}


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def human_bytes(n):
    if n is None:
        return "unknown"
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def table(rows, headers):
    rows = [[str(c) for c in r] for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(headers)]
    out = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)),
           "  ".join("-" * w for w in widths)]
    out += ["  ".join(c.ljust(w) for c, w in zip(r, widths)) for r in rows]
    return "\n".join(out)


def print_inventory(rep):
    print("Drives: %s" % (" ".join(rep["drives"]) or "none"))
    if rep["games"]:
        print("\nGames")
        print(table([(g.get("title_name") or g["folder"], g.get("title_id") or "?", human_bytes(g.get("bytes")),
                      g["path"]) for g in rep["games"]], ["Title", "Title ID", "Size", "Path"]))
    if rep["apps"]:
        print("\nApps")
        print(table([(a["path"], human_bytes(a["bytes"])) for a in rep["apps"]], ["Path", "Size"]))
    for key, label in (("saves", "Saves (UDATA)"), ("tdata", "Title data (TDATA)")):
        if rep[key]:
            print("\n" + label)
            print(table([(s["path"], s.get("name") or "?", "yes" if s["installed"] else "no",
                          human_bytes(s["bytes"])) for s in rep[key]], ["Path", "Title", "Installed", "Size"]))
    if rep["folders"]:
        print("\nFolder sizes")
        print(table([(f["path"], human_bytes(f["bytes"])) for f in rep["folders"]], ["Path", "Size"]))
    for n in rep["notes"]:
        print("\nnote: " + n)
    for w in rep["warnings"]:
        print("warning: " + w)


def print_plan(plan, h):
    print("Plan: %s   %d operation(s), %s" % (plan["kind"], len(plan["ops"]), human_bytes(plan["total_bytes"])))
    if plan["kind"] == "add-game":
        print("Target: %s" % plan["target"])
        print("Free space: %s" % (human_bytes(plan["free_bytes"]) if plan["free_space_known"]
                                  else "unknown (this server cannot report it; check on the console)"))
    for r in plan.get("refused", []):
        print("REFUSED %s: %s" % (r["path"], r["why"]))
    for v in plan.get("violations", []):
        print("PROBLEM %s: %s" % (v["path"], v["problem"]))
    for n in plan.get("notes", []):
        print("NOTE " + n)
    print()
    for op in plan["ops"]:
        size = (" (%d bytes)" % op["bytes"]) if "bytes" in op else ""
        print("  %-6s %s%s" % (op["op"], op["remote"], size))
    print("\nPlan hash: %s" % h)
    if plan.get("violations"):
        print("This plan cannot be applied until the problems above are fixed.")
    else:
        print("To apply exactly this plan: apply <plan file> --approve %s --yes" % h)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="ogxbox_ftp.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--host", help="console address (default: $OGXBOX_HOST)")
    p.add_argument("--user", help="FTP user (default: $OGXBOX_USER)")
    p.add_argument("--password", help="FTP password (default: $OGXBOX_PASS; prefer the variable, "
                   "a flag shows up in your shell history)")
    p.add_argument("--port", type=int, help="FTP port (default: $OGXBOX_PORT or 21)")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="seconds (default 30)")
    p.add_argument("--active", action="store_true", help="use active mode instead of passive")
    p.add_argument("--json", action="store_true", help="print JSON instead of a table")
    p.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH)
    sub = p.add_subparsers(dest="cmd")
    sub.required = True
    sub.add_parser("probe", help="show which drives the server exposes")
    ls = sub.add_parser("ls", help="list one folder")
    ls.add_argument("path")
    inv = sub.add_parser("inventory", help="read-only walk of every drive")
    inv.add_argument("--no-xbe", action="store_true", help="do not read title IDs from default.xbe")
    bk = sub.add_parser("backup", help="copy EEPROM backup, saves and dashboard profile to a local folder")
    bk.add_argument("dest")
    bk.add_argument("--source", action="append", default=[], metavar="LABEL=PATH",
                    help="extra folder to copy, e.g. extra=/E/Apps/MyApp/config")
    tid = sub.add_parser("title-id", help="look up title IDs, or read one from a local default.xbe")
    tid.add_argument("ids", nargs="*")
    tid.add_argument("--xbe", help="path to a local default.xbe")
    tid.add_argument("--csv", action="append", default=[], help="extra title-ID CSV (title_id,name)")
    fx = sub.add_parser("check-fatx", help="check a local folder against FATX limits")
    fx.add_argument("local_dir")
    fx.add_argument("--name", help="folder name it will get on the console")
    pa = sub.add_parser("plan-add-game", help="dry run: plan copying a local game folder")
    pa.add_argument("local_dir")
    pa.add_argument("--drive", default="E", choices=GAME_DRIVES)
    pa.add_argument("--title", help="folder name on the console (default: the local folder name)")
    pa.add_argument("--out", required=True, help="where to write the plan file")
    pc = sub.add_parser("plan-cleanup", help="dry run: plan deleting named folders or files")
    pc.add_argument("paths", nargs="+")
    pc.add_argument("--protect", action="append", default=[], help="extra path to never delete")
    pc.add_argument("--out", required=True)
    ap = sub.add_parser("apply", help="apply a plan you have read")
    ap.add_argument("plan")
    ap.add_argument("--approve", help="the plan hash printed by the plan command")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--free-space-ok", action="store_true",
                    help="you checked free space on the console yourself")
    return p


def _emit(args, data, human):
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        human(data)


def _lock(path):
    lock = path + ".lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise SafetyError("%s exists: another apply is running (or crashed; delete the lock "
                          "after checking)" % lock)
    os.close(fd)
    return lock


def main(argv=None, ftp_factory=None, environ=None):
    args = build_parser().parse_args(argv)
    log = lambda m: print(m, file=sys.stderr)
    try:
        if args.cmd == "title-id":
            table_ = load_title_ids() if not args.csv else load_title_ids([default_title_csv()] + args.csv)
            rows = []
            if args.xbe:
                with open(args.xbe, "rb") as fh:
                    info = parse_xbe(fh.read(XBE_HEAD_BYTES))
                r = lookup_title(info["title_id"], table_)
                r["title_name_in_xbe"] = info["title_name"]
                rows.append(r)
            for i in args.ids:
                rows.append(lookup_title(i, table_))
            _emit(args, rows, lambda rs: print(table(
                [(r["title_id"], r.get("title_name_in_xbe") or r["name"] or "unknown", r["code"] or "")
                 for r in rs], ["Title ID", "Name", "Code"])))
            return 0
        if args.cmd == "check-fatx":
            v = check_fatx_tree(args.local_dir, args.name)
            _emit(args, v, lambda vs: print("\n".join("%s: %s" % (x["path"], x["problem"]) for x in vs)
                                         or "No FATX problems found."))
            return 1 if v else 0
        if args.cmd == "apply":
            plan = read_plan(args.plan)
            if not args.yes or args.approve != plan_hash(plan):
                apply_plan(None, plan, args.approve, yes=args.yes)  # raises the reason
        settings = Settings.from_args(args, environ)
        with XboxFTP(settings, ftp_factory=ftp_factory, log=log) as c:
            if args.cmd == "probe":
                _emit(args, {"banner": c.banner.strip(), "drives": c.probe_drives()},
                      lambda d: print("Server: %s\nDrives: %s" % (d["banner"], " ".join(d["drives"]))))
            elif args.cmd == "ls":
                here, entries = c.list_dir(args.path)
                _emit(args, {"path": here, "entries": entries}, lambda d: print(table(
                    [("dir" if e["is_dir"] else "file", e["size"], e["name"]) for e in d["entries"]],
                    ["Type", "Bytes", "Name"])))
            elif args.cmd == "inventory":
                _emit(args, inventory(c, args.max_depth, read_xbe=not args.no_xbe), print_inventory)
            elif args.cmd == "backup":
                sources = list(DEFAULT_BACKUP_SOURCES)
                for s in args.source:
                    label, _, path = s.partition("=")
                    if not path or not re.match(r"^[A-Za-z0-9_-]+$", label):
                        raise OgxError("--source must look like label=/E/path")
                    sources.append((label, path))
                rep = backup(c, args.dest, sources, args.max_depth)

                def human(r):
                    print("Backup folder: %s (owner-only)" % r["root"])
                    print(table([(x["remote"], x["bytes"], "yes" if x["verified"] else "NO",
                                  "sensitive" if x["sensitive"] else "") for x in r["copied"]],
                                ["Remote", "Bytes", "Verified", ""]))
                    for s in r["skipped"]:
                        print("skipped %s: %s" % (s["source"], s["why"]))
                    for w in r["warnings"]:
                        print("warning: " + w)
                    for e in r["errors"]:
                        print("ERROR %s: %s" % (e["remote"], e["error"]))
                    print("eeprom.bin and 'xbox info.txt' hold your hard drive key: keep this folder "
                          "off cloud sync and out of git.")
                _emit(args, rep, human)
                return 1 if rep["errors"] else 0
            elif args.cmd in ("plan-add-game", "plan-cleanup"):
                if args.cmd == "plan-add-game":
                    plan = plan_add_game(c, args.local_dir, args.drive, args.title)
                else:
                    plan = plan_cleanup(c, args.paths, list(args.protect) + env_protected(), args.max_depth)
                h = write_plan(plan, args.out)
                _emit(args, {"plan": args.out, "hash": h, "content": plan}, lambda d: print_plan(plan, h))
                return 1 if plan.get("violations") else 0
            elif args.cmd == "apply":
                lock = _lock(args.plan)
                try:
                    res = apply_plan(c, plan, args.approve, yes=args.yes,
                                     free_space_ok=args.free_space_ok, log=log)
                finally:
                    os.unlink(lock)
                _emit(args, res, lambda r: print("Applied and verified %d operation(s)." % r["applied"]))
        return 0
    except SafetyError as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return 2
    except (OgxError, ftplib.Error, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
