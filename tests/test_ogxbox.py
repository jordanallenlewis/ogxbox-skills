"""Tests for lib/ogxbox_ftp.py against a fake console. No network."""

import contextlib
import ftplib
import hashlib
import io
import json
import os
import stat
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lib"))
sys.path.insert(0, HERE)

import ogxbox_ftp as ox  # noqa: E402
from fake_xbox import FakeXboxFTP, factory, make_xbe  # noqa: E402

ENV = {"OGXBOX_HOST": "console.example", "OGXBOX_USER": "xbox", "OGXBOX_PASS": "xbox"}


def client_for(fake):
    s = ox.Settings("console.example", "xbox", "xbox")
    return ox.XboxFTP(s, ftp_factory=factory(fake)).connect()


def run_main(argv, fake, environ=ENV):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = ox.main(argv, ftp_factory=factory(fake), environ=dict(environ))
    return code, out.getvalue(), err.getvalue()


def with_timeout(fn, seconds=10):
    box = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as exc:  # pragma: no cover - surfaced below
            box["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise AssertionError("did not finish in %d s (looping?)" % seconds)
    if "error" in box:
        raise box["error"]
    return box.get("value")


class ListParsing(unittest.TestCase):
    def test_dir_and_file(self):
        d = ox.parse_list_line("drwxr-xr-x 1 root  root  0 Jan 01 2002 games")
        self.assertEqual(d, {"name": "games", "is_dir": True, "size": 0, "mtime": "Jan 01 2002"})
        f = ox.parse_list_line("-rw-r--r-- 1 root root 123456 Mar  5 12:34 default.xbe\r\n")
        self.assertFalse(f["is_dir"])
        self.assertEqual(f["size"], 123456)
        self.assertEqual(f["name"], "default.xbe")

    def test_names_with_spaces(self):
        e = ox.parse_list_line("drwxr-xr-x 1 root root 0 Jan 01 2002 Halo  Combat Evolved ")
        self.assertEqual(e["name"], "Halo  Combat Evolved ")
        e = ox.parse_list_line("-rw-r--r-- 1 root root 31 Jan 01 2002 xbox info.txt")
        self.assertEqual(e["name"], "xbox info.txt")

    def test_non_entries(self):
        for line in ("total 12", "", "drwxr-xr-x 1 root root 0 Jan 01 2002 .",
                     "drwxr-xr-x 1 root root 0 Jan 01 2002 ..", "garbage line"):
            self.assertIsNone(ox.parse_list_line(line))


class RemotePaths(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(ox.to_remote("E:\\games\\Halo"), "/E/games/Halo")
        self.assertEqual(ox.to_remote("e:/games/"), "/E/games")
        self.assertEqual(ox.to_remote("/e/games"), "/E/games")
        self.assertEqual(ox.to_remote("/"), "/")

    def test_rejects_dotdot_and_escapes(self):
        for bad in ("/E/games/../../C", "/E/..", "..", "/E/./games", "/E//games",
                    "games/Halo", "", "/EE/games", "/E/ga\nmes", "E:\\games\\..\\x"):
            with self.assertRaises(ox.SafetyError, msg=bad):
                ox.to_remote(bad)

    def test_write_to_c_refused(self):
        for p in ("/C", "/C/xboxdash.xbe", "C:\\xodash", "/c/anything/deep"):
            with self.assertRaises(ox.SafetyError):
                ox.guard_write(p)

    def test_other_read_only_drives_refused(self):
        for p in ("/D/x", "/X/cache", "/Q/userdata", "/P/x", "/E"):
            with self.assertRaises(ox.SafetyError):
                ox.guard_write(p)
        self.assertEqual(ox.guard_write("/E/games/New"), "/E/games/New")


class RootBug(unittest.TestCase):
    def test_fake_reproduces_the_server_bug(self):
        """A naive walker that sends `LIST <path>` never gets anywhere: the
        server keeps listing the current folder, so the path grows
        /E/C/C/C... This proves the fake is faithful."""
        fake = FakeXboxFTP()
        fake.login("xbox", "xbox")
        seen = []

        def naive(path, depth):
            if depth > 25:
                return True
            lines = []
            fake.retrlines("LIST " + path, lines.append)
            for line in lines:
                e = ox.parse_list_line(line)
                if e and e["is_dir"]:
                    seen.append(path + "/" + e["name"])
                    return naive(path + "/" + e["name"], depth + 1)
            return False

        self.assertTrue(naive("/E", 0), "naive walk should hit the depth cap")
        self.assertTrue(seen[-1].startswith("/E/C/C/C/C"))

    def test_inventory_terminates_and_never_lists_a_path(self):
        fake = FakeXboxFTP()
        c = client_for(fake)
        rep = with_timeout(lambda: ox.inventory(c, titles={}))
        lists = [cmd for cmd in fake.log if cmd.upper().startswith("LIST")]
        self.assertTrue(lists)
        self.assertTrue(all(cmd == "LIST" for cmd in lists), "LIST must never carry a path")
        self.assertFalse([cmd for cmd in fake.log if "MLSD" in cmd.upper()])
        self.assertEqual(rep["drives"], ["C", "D", "E", "F", "P", "Q"])
        games = {g["path"]: g for g in rep["games"]}
        self.assertEqual(list(games), ["/E/games/Halo Combat Evolved"])
        halo = games["/E/games/Halo Combat Evolved"]
        self.assertEqual(halo["title_id"], "4d530004")
        self.assertEqual(halo["title_name"], "Halo")
        self.assertEqual(halo["bytes"], 0x1000 + 3000)
        saves = {s["path"]: s for s in rep["saves"]}
        self.assertTrue(saves["/E/UDATA/4d530004"]["installed"])
        self.assertFalse(saves["/E/UDATA/4d530064"]["installed"])
        self.assertEqual(saves["/E/UDATA/4d530064"]["name"], "MS-100")
        self.assertEqual([a["path"] for a in rep["apps"]], ["/E/apps/File Manager"])
        self.assertTrue(any("X, Y, Z" in n for n in rep["notes"]))
        self.assertNotIn("/C/xodash", [f["path"] for f in rep["folders"]])  # C: is not walked

    def test_mlsd_is_refused_by_the_fake(self):
        fake = FakeXboxFTP()
        with self.assertRaises(ftplib.error_perm):
            fake.retrlines("MLSD", lambda l: None)

    def test_missing_drive_is_not_an_error(self):
        fake = FakeXboxFTP()
        c = client_for(fake)
        self.assertEqual(c.probe_drives(["E", "G", "X"]), ["E"])
        self.assertEqual(c.warnings, [])


class LoopGuard(unittest.TestCase):
    def test_cwd_landing_elsewhere_stops_that_branch(self):
        fake = FakeXboxFTP()
        fake.fs["E"]["loop"] = {"inner": {}}
        fake.pwd_override["/e/loop"] = "/E"  # the server lies about where we are
        c = client_for(fake)
        paths = with_timeout(lambda: [p for p, _ in c.walk("/E")])
        self.assertNotIn("/E/loop", paths)
        self.assertNotIn("/E/loop/inner", paths)
        self.assertTrue(any("landed in /E" in w for w in c.warnings))

    def test_same_real_folder_visited_once(self):
        fake = FakeXboxFTP()
        fake.fs["E"]["a"] = {"b": {}}
        fake.pwd_override["/e/a/b"] = "/E/a"
        c = client_for(fake)
        paths = with_timeout(lambda: [p for p, _ in c.walk("/E/a")])
        self.assertEqual(paths, ["/E/a"])

    def test_max_depth(self):
        fake = FakeXboxFTP()
        node = fake.fs["E"]
        for i in range(10):
            node["d%d" % i] = {}
            node = node["d%d" % i]
        c = client_for(fake)
        paths = [p for p, _ in c.walk("/E/d0", max_depth=3)]
        self.assertEqual(len(paths), 4)
        self.assertTrue(any("max depth" in w for w in c.warnings))


class BinaryMode(unittest.TestCase):
    def test_every_transfer_is_binary(self):
        fake = FakeXboxFTP()
        c = client_for(fake)
        fake.type = "A"
        buf = bytearray()
        c.download("/E/games/Halo Combat Evolved/Media/maps/level one.map", buf.extend)
        self.assertEqual(len(buf), 3000)
        c.read_head("/E/games/Halo Combat Evolved/default.xbe", 512)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "f.bin")
            with open(p, "wb") as fh:
                fh.write(b"\x00\xff" * 10)
            fake.type = "A"
            c.upload(p, "/E/games/new.bin")
        self.assertEqual(len(fake.transfers), 3)
        self.assertTrue(all(t == "I" for _, t in fake.transfers), fake.transfers)


class WriteSafety(unittest.TestCase):
    def test_c_refused_in_primitives(self):
        fake = FakeXboxFTP()
        c = client_for(fake)
        with tempfile.NamedTemporaryFile() as fh:
            for fn in (lambda: c.mkdir("/C/new"), lambda: c.upload(fh.name, "/C/xboxdash.xbe"),
                       lambda: c.delete_file("/C/xboxdash.xbe"), lambda: c.remove_dir("/C/xodash")):
                with self.assertRaises(ox.SafetyError):
                    fn()
        self.assertEqual(fake.writes, [])

    def test_one_operation_at_a_time(self):
        fake = FakeXboxFTP()
        c = client_for(fake)
        with c._one_op():
            with self.assertRaises(ox.SafetyError):
                c.mkdir("/E/games/x")
        self.assertEqual(fake.writes, [])

    def test_upload_verify_catches_short_file(self):
        fake = FakeXboxFTP()
        orig = fake.storbinary

        def short(cmd, fp, *a, **k):
            r = orig(cmd, fp)
            fake._here()[cmd.split(" ", 1)[1]] = b"cut"
            return r
        fake.storbinary = short
        c = client_for(fake)
        with tempfile.NamedTemporaryFile() as fh:
            fh.write(b"0123456789")
            fh.flush()
            with self.assertRaises(ox.OgxError) as cm:
                c.upload(fh.name, "/E/games/x.bin")
        self.assertIn("verify failed", str(cm.exception))


def make_game(root, name="My Game", files=None):
    g = os.path.join(root, name)
    os.makedirs(os.path.join(g, "media"))
    with open(os.path.join(g, "default.xbe"), "wb") as fh:
        fh.write(make_xbe(0x41430001, "My Game"))
    for rel, data in (files or {"media/level 1.bin": b"L" * 500}).items():
        with open(os.path.join(g, rel), "wb") as fh:
            fh.write(data)
    return g


class AddGame(unittest.TestCase):
    def test_plan_then_apply_with_approval(self):
        fake = FakeXboxFTP()
        c = client_for(fake)
        with tempfile.TemporaryDirectory() as d:
            g = make_game(d)
            plan = ox.plan_add_game(c, g, "E")
            self.assertEqual(plan["violations"], [])
            self.assertFalse(plan["free_space_known"])
            remotes = [o["remote"] for o in plan["ops"]]
            self.assertEqual(remotes, ["/E/games/My Game", "/E/games/My Game/media",
                                       "/E/games/My Game/default.xbe",
                                       "/E/games/My Game/media/level 1.bin"])
            self.assertEqual(fake.writes, [], "planning must not write")
            h = ox.plan_hash(plan)
            with self.assertRaises(ox.SafetyError):
                ox.apply_plan(c, plan, h, yes=False)
            with self.assertRaises(ox.SafetyError):
                ox.apply_plan(c, plan, "0" * 64, yes=True)
            with self.assertRaises(ox.SafetyError):  # free space unknown, not confirmed
                ox.apply_plan(c, plan, h, yes=True)
            self.assertEqual(fake.writes, [])
            res = ox.apply_plan(c, plan, h, yes=True, free_space_ok=True)
            self.assertEqual(res["applied"], 4)
            self.assertEqual(len(fake.fs["E"]["games"]["My Game"]["media"]["level 1.bin"]), 500)

    def test_free_space_reported(self):
        fake = FakeXboxFTP()
        fake.free_bytes = 100
        c = client_for(fake)
        with tempfile.TemporaryDirectory() as d:
            plan = ox.plan_add_game(c, make_game(d), "E")
        self.assertTrue(plan["free_space_known"])
        self.assertTrue(any("free" in v["problem"] for v in plan["violations"]))

    def test_needs_default_xbe(self):
        c = client_for(FakeXboxFTP())
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ox.OgxError):
                ox.plan_add_game(c, d, "E")

    def test_existing_target_is_a_problem(self):
        fake = FakeXboxFTP()
        fake.fs["E"]["games"]["My Game"] = {}
        c = client_for(fake)
        with tempfile.TemporaryDirectory() as d:
            plan = ox.plan_add_game(c, make_game(d), "E")
        self.assertTrue(any("already exists" in v["problem"] for v in plan["violations"]))

    def test_cli_apply_without_approval_refused(self):
        fake = FakeXboxFTP()
        with tempfile.TemporaryDirectory() as d:
            g = make_game(d)
            planfile = os.path.join(d, "plan.json")
            code, out, _ = run_main(["plan-add-game", g, "--out", planfile], fake)
            self.assertEqual(code, 0)
            self.assertIn("/E/games/My Game/media/level 1.bin", out)
            h = ox.plan_hash(ox.read_plan(planfile))
            self.assertIn(h, out)
            code, _, err = run_main(["apply", planfile], fake)
            self.assertEqual(code, 2)
            self.assertIn("refused", err)
            code, _, err = run_main(["apply", planfile, "--yes"], fake)
            self.assertEqual(code, 2)
            code, _, err = run_main(["apply", planfile, "--approve", h], fake)
            self.assertEqual(code, 2)
            self.assertEqual(fake.writes, [])
            code, out, err = run_main(["apply", planfile, "--approve", h, "--yes", "--free-space-ok"], fake)
            self.assertEqual(code, 0, err)
            self.assertIn("verified 4", out)
            self.assertFalse(os.path.exists(planfile + ".lock"))

    def test_edited_plan_fails_hash(self):
        fake = FakeXboxFTP()
        c = client_for(fake)
        with tempfile.TemporaryDirectory() as d:
            plan = ox.plan_add_game(c, make_game(d), "E")
            h = ox.plan_hash(plan)
            plan["ops"][0]["remote"] = "/E/games/Other"
            with self.assertRaises(ox.SafetyError):
                ox.apply_plan(c, plan, h, yes=True, free_space_ok=True)
        self.assertEqual(fake.writes, [])


class Fatx(unittest.TestCase):
    def test_limits(self):
        self.assertEqual(ox.fatx_name_problems("a" * 42), [])
        self.assertTrue(ox.fatx_name_problems("a" * 43))
        self.assertEqual(ox.fatx_name_problems("Halo - Combat Evolved (v1.0) [!]"), [])
        for bad in ("what?.xbe", "a*b", "a:b", "a/b", "caf\u00e9", 'a"b', "a<b", "a|b", "a+b", "a,b"):
            self.assertTrue(ox.fatx_name_problems(bad), bad)

    def test_tree_reports_every_violation(self):
        with tempfile.TemporaryDirectory() as d:
            g = make_game(d, files={"media/" + "n" * 39 + ".bin": b"x", "media/bad*name.bin": b"y"})
            big = os.path.join(g, "huge.iso")
            with open(big, "wb") as fh:
                fh.truncate(ox.FATX_MAX_FILE + 1)  # sparse: takes no real space
            v = ox.check_fatx_tree(g)
            problems = " | ".join("%s: %s" % (x["path"], x["problem"]) for x in v)
            self.assertIn("43 characters", problems)
            self.assertIn("bad*name.bin", problems)
            self.assertIn("huge.iso", problems)
            self.assertEqual(len(v), 3)
            fake = FakeXboxFTP()
            c = client_for(fake)
            plan = ox.plan_add_game(c, g, "E")
            self.assertEqual(len(plan["violations"]), 3)
            with self.assertRaises(ox.SafetyError):
                ox.apply_plan(c, plan, ox.plan_hash(plan), yes=True, free_space_ok=True)
            self.assertEqual(fake.writes, [])

    def test_exactly_4gb_minus_one_is_fine(self):
        with tempfile.TemporaryDirectory() as d:
            g = make_game(d)
            with open(os.path.join(g, "edge.bin"), "wb") as fh:
                fh.truncate(ox.FATX_MAX_FILE)
            self.assertEqual(ox.check_fatx_tree(g), [])


class Cleanup(unittest.TestCase):
    PROTECTED = ["/E/Backups", "/E/Backups/EEPROM/eeprom.bin", "/C/xodash", "/E/UDATA", "/E/TDATA",
                 "/E", "/E/UDATA/4d530004", "/E/TDATA/4d530004", "/X/cache", "/E/NKPatcher",
                 "/E/NKPatcher/settings.ini", "/E/Dashboards/XBMC4Gamers", "/Q/default.xbe", "/P",
                 "/E/games/../Backups"]

    def test_never_delete_refused_in_plan(self):
        fake = FakeXboxFTP()
        c = client_for(fake)
        plan = ox.plan_cleanup(c, self.PROTECTED)
        self.assertEqual(plan["ops"], [])
        self.assertEqual(len(plan["refused"]), len(self.PROTECTED))
        self.assertIn("4d530004", plan["protected_titles"])

    def test_forged_plan_refused_at_apply(self):
        fake = FakeXboxFTP()
        c = client_for(fake)
        for target in ("/E/Backups/EEPROM/eeprom.bin", "/E/UDATA/4d530004/save one/SaveMeta.xbx",
                       "/C/xboxdash.xbe"):
            plan = {"format": ox.PLAN_FORMAT, "kind": "cleanup", "ops": [
                {"op": "delete", "remote": target, "bytes": 1}], "violations": [],
                "protected_titles": [], "total_bytes": 1}
            with self.assertRaises(ox.SafetyError, msg=target):
                ox.apply_plan(c, plan, ox.plan_hash(plan), yes=True)
        self.assertEqual(fake.writes, [])

    def test_primitive_refuses_protected(self):
        c = client_for(FakeXboxFTP())
        with self.assertRaises(ox.SafetyError):
            c.delete_file("/E/Backups/EEPROM/eeprom.bin")
        with self.assertRaises(ox.SafetyError):
            c.remove_dir("/E/UDATA")
        with self.assertRaises(ox.SafetyError):
            c.delete_file("/E/NKPatcher/settings.ini")

    def test_user_protect_list(self):
        c = client_for(FakeXboxFTP())
        plan = ox.plan_cleanup(c, ["/E/games/Not A Game"], extra_protected=["/E/games/Not A Game"])
        self.assertEqual(plan["ops"], [])

    def test_named_uninstalled_save_can_be_removed(self):
        fake = FakeXboxFTP()
        c = client_for(fake)
        plan = ox.plan_cleanup(c, ["/E/UDATA/4d530064", "/E/games/Not A Game"])
        self.assertEqual(plan["refused"], [])
        ops = [(o["op"], o["remote"]) for o in plan["ops"]]
        self.assertIn(("delete", "/E/UDATA/4d530064/old save/SaveMeta.xbx"), ops)
        self.assertLess(ops.index(("rmdir", "/E/UDATA/4d530064/old save")),
                        ops.index(("rmdir", "/E/UDATA/4d530064")))
        self.assertTrue(plan["notes"])
        self.assertEqual(fake.writes, [])
        res = ox.apply_plan(c, plan, ox.plan_hash(plan), yes=True)
        self.assertEqual(res["applied"], len(plan["ops"]))
        self.assertNotIn("4d530064", fake.fs["E"]["UDATA"])
        self.assertNotIn("Not A Game", fake.fs["E"]["games"])
        self.assertIn("4d530004", fake.fs["E"]["UDATA"])

    def test_apply_rechecks_installed_games(self):
        fake = FakeXboxFTP()
        c = client_for(fake)
        plan = ox.plan_cleanup(c, ["/E/TDATA/4d530064"])
        self.assertTrue(plan["ops"])
        # the user installs the game between plan and apply
        fake.fs["E"]["games"]["Game 100"] = {"default.xbe": make_xbe(0x4D530064, "Game 100")}
        with self.assertRaises(ox.SafetyError):
            ox.apply_plan(c, plan, ox.plan_hash(plan), yes=True)
        self.assertEqual(fake.writes, [])


class TitleIds(unittest.TestCase):
    def test_bundled_list(self):
        t = ox.load_title_ids([ox.default_title_csv()])
        self.assertEqual(ox.lookup_title("4D530004", t)["name"], "Halo: Combat Evolved")
        self.assertEqual(ox.lookup_title("0x45410017", t)["code"], "EA-023")

    def test_unknown(self):
        r = ox.lookup_title("4d530064", ox.load_title_ids([ox.default_title_csv()]))
        self.assertIsNone(r["name"])
        self.assertFalse(r["known"])
        self.assertEqual(r["code"], "MS-100")
        self.assertIsNone(ox.describe_title_id("00000001"))
        with self.assertRaises(ox.OgxError):
            ox.normalize_title_id("xyz")

    def test_user_csv_wins(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "mine.csv")
            with open(p, "w") as fh:
                fh.write("title_id,name\n4d530004,My Name\n4d530064,Another\nbad,row\n")
            t = ox.load_title_ids([ox.default_title_csv(), p])
        self.assertEqual(t["4d530004"], "My Name")
        self.assertEqual(t["4d530064"], "Another")

    def test_parse_xbe(self):
        info = ox.parse_xbe(make_xbe(0x4D530064, "Some Game"))
        self.assertEqual(info, {"title_id": "4d530064", "title_name": "Some Game"})
        with self.assertRaises(ox.OgxError):
            ox.parse_xbe(b"MZ" + b"\x00" * 400)

    def test_cli_reads_local_xbe(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "default.xbe")
            with open(p, "wb") as fh:
                fh.write(make_xbe(0x4D530004, "Halo"))
            code, out, _ = run_main(["--json", "title-id", "--xbe", p, "deadbeef"], FakeXboxFTP(), environ={})
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertEqual(rows[0]["title_id"], "4d530004")
        self.assertEqual(rows[0]["title_name_in_xbe"], "Halo")
        self.assertFalse(rows[1]["known"])


class Backup(unittest.TestCase):
    def test_private_files_and_nothing_printed(self):
        fake = FakeXboxFTP()
        secrets = [fake.fs["E"]["Backups"]["EEPROM"]["eeprom.bin"],
                   fake.fs["E"]["Backups"]["EEPROM"]["xbox info.txt"]]
        with tempfile.TemporaryDirectory() as d:
            dest = os.path.join(d, "xbox-backups")
            for argv in (["backup", dest], ["--json", "backup", dest]):
                code, out, err = run_main(argv, fake)
                self.assertEqual(code, 0, err)
                text = out + err
                for s in secrets:
                    self.assertNotIn(s.decode("latin-1")[:12], text)
                    self.assertNotIn(hashlib.sha256(s).hexdigest(), text)
                    self.assertNotIn(hashlib.md5(s).hexdigest(), text)
                self.assertNotIn("SECRET", text)
            self.assertEqual(stat.S_IMODE(os.stat(dest).st_mode), 0o700)
            roots = [os.path.join(dest, n) for n in os.listdir(dest)]
            self.assertTrue(roots)
            files = 0
            for root in roots:
                self.assertTrue(os.path.exists(os.path.join(root, ".gitignore")))
                for dp, dns, fns in os.walk(root):
                    self.assertEqual(stat.S_IMODE(os.stat(dp).st_mode), 0o700, dp)
                    for f in fns:
                        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(dp, f)).st_mode), 0o600, f)
                        files += 1
                eeprom = os.path.join(root, "eeprom", "eeprom.bin")
                with open(eeprom, "rb") as fh:
                    self.assertEqual(fh.read(), secrets[0])
                self.assertTrue(os.path.exists(os.path.join(root, "eeprom", "xbox info.txt")))
                self.assertTrue(os.path.exists(os.path.join(root, "saves", "4d530004", "save one",
                                                            "SaveMeta.xbx")))
                self.assertTrue(os.path.exists(os.path.join(root, "dashboard-profile", "guisettings.xml")))
            self.assertEqual(files, 12)  # two separate backups of 6 files
        self.assertEqual(fake.writes, [], "backup must not write to the console")
        self.assertTrue(all(t == "I" for _, t in fake.transfers))

    def test_size_mismatch_reported(self):
        fake = FakeXboxFTP()
        orig = fake.retrbinary

        def short(cmd, cb, *a, **k):
            return orig(cmd, lambda data: cb(data[:-1] if data else data))
        fake.retrbinary = short
        c = client_for(fake)
        with tempfile.TemporaryDirectory() as d:
            rep = ox.backup(c, d, [("eeprom", "/E/Backups/EEPROM")])
        self.assertTrue(rep["errors"])
        self.assertFalse(any(x["verified"] for x in rep["copied"]))

    def test_warns_inside_git(self):
        c = client_for(FakeXboxFTP())
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, ".git"))
            rep = ox.backup(c, os.path.join(d, "b"), [("eeprom", "/E/Backups/EEPROM")])
        self.assertTrue(any("git" in w for w in rep["warnings"]))


class Credentials(unittest.TestCase):
    def test_password_hidden(self):
        s = ox.Settings("h", "u", "hunter2-secret")
        self.assertNotIn("hunter2", repr(s))
        self.assertNotIn("hunter2", str(s))

    def test_env_and_flags(self):
        class A(object):
            host = None
            user = None
            password = None
            port = None
        s = ox.Settings.from_args(A(), {"OGXBOX_HOST": "h", "OGXBOX_USER": "u", "OGXBOX_PASS": "p",
                                        "OGXBOX_PORT": "2121"})
        self.assertEqual((s.host, s.user, s.password, s.port), ("h", "u", "p", 2121))
        A.host = "flag-host"
        self.assertEqual(ox.Settings.from_args(A(), {"OGXBOX_USER": "u", "OGXBOX_PASS": "p"}).host,
                         "flag-host")
        with self.assertRaises(ox.OgxError):
            ox.Settings.from_args(type("B", (), {})(), {})

    def test_missing_settings_exit_cleanly(self):
        code, out, err = run_main(["probe"], FakeXboxFTP(), environ={})
        self.assertEqual(code, 1)
        self.assertIn("OGXBOX_HOST", err)

    def test_bad_password_not_echoed(self):
        env = dict(ENV, OGXBOX_PASS="wrong-pass-value")
        code, out, err = run_main(["probe"], FakeXboxFTP(), environ=env)
        self.assertEqual(code, 1)
        self.assertNotIn("wrong-pass-value", out + err)

    def test_no_credentials_in_source(self):
        src = open(os.path.join(HERE, "..", "lib", "ogxbox_ftp.py")).read()
        self.assertNotIn('password="xbox"', src)
        self.assertNotIn("password='xbox'", src)


class Cli(unittest.TestCase):
    def test_probe_and_ls(self):
        fake = FakeXboxFTP()
        code, out, _ = run_main(["probe"], fake)
        self.assertEqual(code, 0)
        self.assertIn("XBMC:FileZilla", out)
        code, out, _ = run_main(["--json", "ls", "E:\\games"], fake)
        self.assertEqual(code, 0)
        names = [e["name"] for e in json.loads(out)["entries"]]
        self.assertIn("Halo Combat Evolved", names)

    def test_inventory_table(self):
        code, out, _ = run_main(["inventory"], FakeXboxFTP())
        self.assertEqual(code, 0)
        self.assertIn("Halo", out)
        self.assertIn("/E/UDATA/4d530064", out)


if __name__ == "__main__":
    unittest.main()
