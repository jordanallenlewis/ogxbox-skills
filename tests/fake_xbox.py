"""A stand-in for ftplib.FTP that behaves like the XBMC FileZilla server on a
softmodded console, including its quirks:

- LIST ignores any path argument and lists the current directory.
- MLSD is not supported (500).
- The FTP root lists the drives (C, E, F, ...) as folders.
- AVBL (free space) is not supported unless the test sets `free_bytes`.

No sockets, no network. The file system is a nested dict: a dict is a
folder, bytes are a file.
"""

import ftplib
import io
import struct

BANNER = "220 XBMC:FileZilla 1.5.6"


def make_xbe(title_id, title_name, pad=0x1000):
    """Bytes that look like the start of a default.xbe (header + certificate)."""
    base = 0x00010000
    cert_off = 0x180
    data = bytearray(pad)
    data[0:4] = b"XBEH"
    struct.pack_into("<I", data, 0x104, base)
    struct.pack_into("<I", data, 0x118, base + cert_off)
    struct.pack_into("<I", data, cert_off + 0x00, 0x1D0)
    struct.pack_into("<I", data, cert_off + 0x08, title_id)
    name = title_name.encode("utf-16-le")[:80]
    data[cert_off + 0x0C: cert_off + 0x0C + len(name)] = name
    return bytes(data)


def default_fs():
    return {
        "C": {
            "xboxdash.xbe": b"x" * 100,
            "xodash": {"xonlinedash.xbe": b"y" * 50},
        },
        "D": {},
        "E": {
            "Backups": {
                "EEPROM": {
                    "eeprom.bin": b"SECRET-EEPROM-" + b"\x01" * 242,
                    "xbox info.txt": b"HDD Key: SECRET-HDD-KEY-VALUE\r\n",
                },
            },
            "games": {
                "Halo Combat Evolved": {
                    "default.xbe": make_xbe(0x4D530004, "Halo"),
                    "Media": {"maps": {"level one.map": b"m" * 3000}},
                },
                "Not A Game": {"readme.txt": b"hello"},
            },
            "apps": {"File Manager": {"default.xbe": make_xbe(0x00000000, "Files")}},
            "UDATA": {
                "4d530004": {"save one": {"SaveMeta.xbx": b"s" * 40}},
                "4d530064": {"old save": {"SaveMeta.xbx": b"o" * 70}},
            },
            "TDATA": {
                "4d530004": {"cache.bin": b"t" * 10},
                "4d530064": {"settings.bin": b"u" * 20},
            },
            "NKPatcher": {"settings.ini": b"[nkp]"},
            "Dashboards": {"XBMC4Gamers": {"default.xbe": make_xbe(0x00000001, "XBMC4Gamers")}},
        },
        "F": {"games": {}},
        "P": {"guisettings.xml": b"<settings/>"},
        "Q": {"default.xbe": make_xbe(0x00000001, "XBMC4Gamers")},
    }


class FakeConn(object):
    def __init__(self, data):
        self._buf = io.BytesIO(data)
        self.closed = False

    def recv(self, n):
        return self._buf.read(n)

    def close(self):
        self.closed = True


class FakeXboxFTP(object):
    def __init__(self, fs=None, user="xbox", password="xbox", timeout=None):
        self.fs = default_fs() if fs is None else fs
        self._user, self._password = user, password
        self.cwd_parts = []
        self.type = "A"
        self.log = []            # every command, in order
        self.transfers = []      # (command, type at the time)
        self.writes = []         # (command, absolute path)
        self.pwd_override = {}   # lower-case path -> what PWD should claim
        self.free_bytes = None
        self.passive = None
        self.timeout = timeout

    # helpers ---------------------------------------------------------------

    def _split(self, path):
        if path.startswith("/"):
            parts = [p for p in path.split("/") if p]
        else:
            parts = self.cwd_parts + [p for p in path.split("/") if p]
        out = []
        for p in parts:
            if p == "..":
                if out:
                    out.pop()
            elif p != ".":
                out.append(p)
        return out

    def _lookup(self, parts):
        node, real = self.fs, []
        for p in parts:
            if not isinstance(node, dict):
                return None, None
            match = [k for k in node if k.lower() == p.lower()]
            if not match:
                return None, None
            real.append(match[0])
            node = node[match[0]]
        return node, real

    def _here(self):
        node, _ = self._lookup(self.cwd_parts)
        return node

    def _abs(self, name):
        return "/" + "/".join(self._split(name))

    # ftplib.FTP surface ------------------------------------------------------

    def connect(self, host, port=21, timeout=None):
        self.log.append("CONNECT")
        return BANNER

    def login(self, user, passwd):
        self.log.append("USER")
        if user != self._user or passwd != self._password:
            raise ftplib.error_perm("530 Login or password incorrect!")
        return "230 Logged on"

    def set_pasv(self, val):
        self.passive = val

    def cwd(self, path):
        self.log.append("CWD " + path)
        node, real = self._lookup(self._split(path))
        if not isinstance(node, dict):
            raise ftplib.error_perm("550 CWD failed. \"%s\": directory not found." % path)
        self.cwd_parts = real
        return "250 CWD successful."

    def pwd(self):
        self.log.append("PWD")
        here = "/" + "/".join(self.cwd_parts)
        return self.pwd_override.get(here.lower(), here)

    def retrlines(self, cmd, callback=None):
        self.log.append(cmd)
        if cmd.upper().startswith("MLSD"):
            raise ftplib.error_perm("500 Syntax error, command unrecognized.")
        if not cmd.upper().startswith("LIST"):
            raise ftplib.error_perm("500 Syntax error, command unrecognized.")
        # The quirk: any argument is ignored; this lists the CURRENT folder.
        node = self._here()
        for name in sorted(node):
            child = node[name]
            if isinstance(child, dict):
                line = "drwxr-xr-x 1 root  root  %12d Jan 01 2002 %s" % (0, name)
            else:
                line = "-rw-r--r-- 1 root  root  %12d Jan 01 2002 %s" % (len(child), name)
            callback(line)
        return "226 Transfer OK"

    def sendcmd(self, cmd):
        self.log.append(cmd)
        if cmd.upper().startswith("AVBL") and self.free_bytes is not None:
            return "213 %d" % self.free_bytes
        raise ftplib.error_perm("500 Syntax error, command unrecognized.")

    def voidcmd(self, cmd):
        self.log.append(cmd)
        if cmd.upper() == "TYPE I":
            self.type = "I"
        elif cmd.upper() == "TYPE A":
            self.type = "A"
        else:
            raise ftplib.error_perm("500 Syntax error, command unrecognized.")
        return "200 Type set"

    def _file(self, name):
        node, _ = self._lookup(self._split(name))
        if not isinstance(node, (bytes, bytearray)):
            raise ftplib.error_perm("550 File not found")
        return node

    def retrbinary(self, cmd, callback, blocksize=8192, rest=None):
        self.log.append(cmd)
        self.transfers.append((cmd, self.type))
        data = self._file(cmd.split(" ", 1)[1])
        for i in range(0, len(data), blocksize):
            callback(data[i:i + blocksize])
        return "226 Transfer OK"

    def transfercmd(self, cmd, rest=None):
        self.log.append(cmd)
        self.transfers.append((cmd, self.type))
        return FakeConn(self._file(cmd.split(" ", 1)[1]))

    def voidresp(self):
        return "226 Transfer OK"

    def storbinary(self, cmd, fp, blocksize=8192, callback=None, rest=None):
        self.log.append(cmd)
        self.transfers.append((cmd, self.type))
        name = cmd.split(" ", 1)[1]
        self.writes.append(("STOR", self._abs(name)))
        self._here()[name] = fp.read()
        return "226 Transfer OK"

    def mkd(self, name):
        self.log.append("MKD " + name)
        self.writes.append(("MKD", self._abs(name)))
        self._here()[name] = {}
        return self._abs(name)

    def delete(self, name):
        self.log.append("DELE " + name)
        self.writes.append(("DELE", self._abs(name)))
        node = self._here()
        if not isinstance(node.get(name), (bytes, bytearray)):
            raise ftplib.error_perm("550 File not found")
        del node[name]
        return "250 Deleted"

    def rmd(self, name):
        self.log.append("RMD " + name)
        self.writes.append(("RMD", self._abs(name)))
        node = self._here()
        if not isinstance(node.get(name), dict) or node[name]:
            raise ftplib.error_perm("550 Directory not empty")
        del node[name]
        return "250 Removed"

    def quit(self):
        self.log.append("QUIT")

    def close(self):
        pass


def factory(fake):
    """An ftp_factory for XboxFTP that always returns `fake`."""
    def make(timeout=None):
        fake.timeout = timeout
        return fake
    return make
