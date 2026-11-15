import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import zipfile
from contextlib import redirect_stdout
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import modpack_dl  # noqa: E402

os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1"


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = modpack_dl.main(argv)
    return code, buf.getvalue()


class ModpackDlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        srv = self.root / "srv"
        srv.mkdir()
        self.data = {"a.jar": b"A" * 300, "c.jar": b"C" * 300}
        for n, d in self.data.items():
            (srv / n).write_bytes(d)
        handler = partial(QuietHandler, directory=str(srv))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def entry(self, name, path, hash_of=None, env=None):
        d = self.data[hash_of or name]
        e = {"path": path, "hashes": {"sha1": hashlib.sha1(d).hexdigest(),
                                      "sha512": hashlib.sha512(d).hexdigest()},
             "downloads": [f"http://127.0.0.1:{self.port}/{name}"]}
        if env:
            e["env"] = env
        return e

    def mrpack(self, files, extra=None):
        p = self.root / "t.mrpack"
        idx = {"formatVersion": 1, "name": "T", "versionId": "1",
               "dependencies": {"minecraft": "1.20.1", "fabric-loader": "0.15.0"}, "files": files}
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("modrinth.index.json", json.dumps(idx))
            z.writestr("overrides/config/x.toml", "a=1")
            for k, v in (extra or {}).items():
                z.writestr(k, v)
        return p

    def args(self, pack, out):
        return [str(pack), "-o", str(out), "--allow-host", "127.0.0.1"]

    def test_downloads_verifies_and_extracts(self):
        pack = self.mrpack([self.entry("a.jar", "mods/a.jar")])
        out = self.root / "out"
        code, text = run(self.args(pack, out))
        self.assertEqual(code, 0, text)
        self.assertEqual((out / "mods/a.jar").read_bytes(), self.data["a.jar"])
        self.assertTrue((out / "config/x.toml").exists())
        code, text = run(self.args(pack, out))  # second run skips
        self.assertEqual(code, 0)
        self.assertIn("SKIP", text)

    def test_hash_mismatch_fails_and_leaves_no_file(self):
        pack = self.mrpack([self.entry("c.jar", "mods/bad.jar", hash_of="a.jar")])
        out = self.root / "out"
        code, text = run(self.args(pack, out))
        self.assertEqual(code, 1)
        self.assertIn("hash mismatch", text)
        self.assertFalse((out / "mods/bad.jar").exists())

    def test_server_only_file_skipped_on_client(self):
        pack = self.mrpack([self.entry("a.jar", "mods/a.jar",
                                       env={"client": "unsupported", "server": "required"})])
        out = self.root / "out"
        code, _ = run(self.args(pack, out))
        self.assertEqual(code, 0)
        self.assertFalse((out / "mods/a.jar").exists())

    def test_path_traversal_in_files_rejected(self):
        pack = self.mrpack([self.entry("a.jar", "../../evil.jar")])
        code, text = run(self.args(pack, self.root / "out"))
        self.assertEqual(code, 2)
        self.assertIn("unsafe path", text)
        self.assertFalse((self.root.parent / "evil.jar").exists())

    def test_zip_slip_in_overrides_rejected_before_download(self):
        pack = self.mrpack([self.entry("a.jar", "mods/a.jar")],
                           extra={"overrides/../../x.txt": "x"})
        out = self.root / "out"
        code, _ = run(self.args(pack, out))
        self.assertEqual(code, 2)
        self.assertFalse((out / "mods/a.jar").exists())

    def test_host_not_allowed_without_flag(self):
        pack = self.mrpack([self.entry("a.jar", "mods/a.jar")])
        code, text = run([str(pack), "-o", str(self.root / "out")])
        self.assertEqual(code, 1)
        self.assertIn("host not allowed", text)

    def test_curseforge_requires_key(self):
        p = self.root / "cf.zip"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("manifest.json", json.dumps({
                "minecraft": {"version": "1.20.1", "modLoaders": [{"id": "forge-47.1.0"}]},
                "name": "CF", "version": "1", "overrides": "overrides",
                "files": [{"projectID": 1, "fileID": 2, "required": True}]}))
        os.environ.pop("CF_API_KEY", None)
        os.environ.pop("CURSEFORGE_API_KEY", None)
        code, text = run([str(p), "-o", str(self.root / "out")])
        self.assertEqual(code, 2)
        self.assertIn("API key", text)

    def test_not_a_modpack(self):
        p = self.root / "x.zip"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("hello.txt", "hi")
        code, text = run([str(p), "-o", str(self.root / "out")])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
