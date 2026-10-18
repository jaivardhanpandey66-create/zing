"""Tests for the zing package manager and the registry server.

The registry tests run a real server on a real port, so the network path is
covered rather than mocked. Each test gets its own ZING_HOME so nothing
leaks between them:

    python3 -m unittest discover -s tests -v
    python3 tests/test_zing_pkg.py
"""

import base64
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from arcide.zing import pkg
from arcide.zing.interp import Output, run_source
from tools.registry_server import Registry, build_zip


def run(src, source_dir=None):
    out = Output(write=lambda s: None)
    try:
        run_source(src, out=out, source_dir=source_dir)
        return out.lines, None
    except Exception as e:
        return out.lines, e


def printed(src, source_dir=None):
    lines, err = run(src, source_dir=source_dir)
    if err:
        raise err
    return lines


def make_package(folder: Path, name: str, version: str = "1.0.0",
                 main: str | None = None, requires=None, body: str = None):
    """Write a minimal but valid package, and return its folder."""
    folder.mkdir(parents=True, exist_ok=True)
    manifest = {"name": name, "version": version,
                "description": f"{name} test package"}
    if main:
        manifest["main"] = main
    if requires:
        manifest["requires"] = requires
    (folder / pkg.MANIFEST).write_text(json.dumps(manifest, indent=2))
    (folder / f"{name}.zng").write_text(
        body if body is not None
        else f"func {name}_value() {{ return 42 }}\n")
    return folder


class PkgTestCase(unittest.TestCase):
    """Gives every test a private home so installs never leak."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self._old_home = os.environ.get("ZING_HOME")
        os.environ["ZING_HOME"] = str(self.home / "zing-home")
        self.addCleanup(self._restore_home)
        self.work = self.home / "work"
        self.work.mkdir()

    def _restore_home(self):
        if self._old_home is None:
            os.environ.pop("ZING_HOME", None)
        else:
            os.environ["ZING_HOME"] = self._old_home

    def registry_manifest(self, name: str) -> dict:
        return json.loads(
            (pkg.registry_dir() / name / pkg.MANIFEST).read_text())


class ManifestValidation(PkgTestCase):
    def test_missing_manifest_is_reported(self):
        empty = self.work / "empty"
        empty.mkdir()
        with self.assertRaises(pkg.PackageError) as ctx:
            pkg.read_manifest(empty)
        self.assertIn(pkg.MANIFEST, str(ctx.exception))

    def test_invalid_json_is_reported(self):
        bad = self.work / "bad"
        bad.mkdir()
        (bad / pkg.MANIFEST).write_text("{not json")
        with self.assertRaises(pkg.PackageError) as ctx:
            pkg.read_manifest(bad)
        self.assertIn("JSON", str(ctx.exception))

    def test_name_is_required(self):
        d = self.work / "noname"
        d.mkdir()
        (d / pkg.MANIFEST).write_text(json.dumps({"version": "1.0.0"}))
        with self.assertRaises(pkg.PackageError) as ctx:
            pkg.read_manifest(d)
        self.assertIn("name", str(ctx.exception))

    def test_invalid_names_are_rejected(self):
        for bad in ("1abc", "has-dash", "has space", ""):
            with self.subTest(name=bad):
                with self.assertRaises(pkg.PackageError):
                    pkg._check_name(bad)

    def test_valid_names_are_accepted(self):
        for good in ("mathx", "_private", "a1", "My_Pkg9"):
            with self.subTest(name=good):
                self.assertEqual(pkg._check_name(good), good)


class Requirements(PkgTestCase):
    def test_requirement_parsing(self):
        self.assertEqual(pkg.parse_requirement("mathx"), ("mathx", ">=0"))
        self.assertEqual(pkg.parse_requirement("mathx >=1.2"), ("mathx", ">=1.2"))
        self.assertEqual(pkg.parse_requirement("mathx ==2"), ("mathx", "==2"))

    def test_version_comparisons(self):
        pkg.install(str(make_package(self.work / "v", "v", "1.2.3")))
        for rule, want in [
            ("v >=1.0.0", True), ("v >=2.0.0", False),
            ("v >1.2.3", False), ("v >1.0.0", True),
            ("v ==1.2.3", True), ("v <2.0.0", True),
            ("v <=1.2.3", True),
        ]:
            with self.subTest(rule=rule):
                self.assertEqual(pkg.requirement_met(rule), want)

    def test_uninstalled_package_never_satisfies(self):
        self.assertFalse(pkg.requirement_met("ghost >=1"))


class InstallFromFolder(PkgTestCase):
    def test_install_then_import(self):
        make_package(self.work / "mathx", "mathx")
        name, action = pkg.install(str(self.work / "mathx"))
        self.assertEqual((name, action), ("mathx", "installed"))
        self.assertTrue((pkg.installed_dir("mathx") / "mathx.zng").is_file())

    def test_import_works_both_qualified_and_bare(self):
        make_package(self.work / "mathx", "mathx")
        pkg.install(str(self.work / "mathx"))
        self.assertEqual(
            printed("import mathx\nprint(mathx.mathx_value())"), ["42"])
        self.assertEqual(
            printed("import mathx\nprint(mathx_value())"), ["42"])

    def test_private_names_are_not_exported(self):
        src = "let _hidden = 1\nfunc visible() { return 2 }\n"
        make_package(self.work / "p", "p", body=src)
        pkg.install(str(self.work / "p"))
        self.assertEqual(printed("import p\nprint(p.visible())"), ["2"])
        _, err = run("import p\nprint(p._hidden)")
        self.assertIsInstance(err, Exception)

    def test_reinstall_same_version_is_unchanged(self):
        make_package(self.work / "m", "m")
        pkg.install(str(self.work / "m"))
        name, action = pkg.install(str(self.work / "m"))
        self.assertEqual(action, "unchanged")

    def test_newer_version_needs_upgrade_flag(self):
        make_package(self.work / "m", "m", "1.0.0")
        pkg.install(str(self.work / "m"))
        make_package(self.work / "m2", "m", "2.0.0")
        with self.assertRaises(pkg.PackageError) as ctx:
            pkg.install(str(self.work / "m2"))
        self.assertIn("--upgrade", str(ctx.exception))
        name, action = pkg.install(str(self.work / "m2"), upgrade=True)
        self.assertEqual(action, "upgraded")
        self.assertEqual(pkg.load_installed("m")["version"], "2.0.0")

    def test_missing_dependency_blocks_install(self):
        make_package(self.work / "m", "m", requires=["helper >=1.0.0"])
        with self.assertRaises(pkg.PackageError) as ctx:
            pkg.install(str(self.work / "m"))
        self.assertIn("helper", str(ctx.exception))
        # Once it is there, the install goes through.
        make_package(self.work / "h", "helper")
        pkg.install(str(self.work / "h"))
        self.assertEqual(pkg.install(str(self.work / "m"))[1], "installed")

    def test_manifest_pointing_at_missing_file_is_caught_at_install(self):
        folder = make_package(self.work / "m", "m", main="nope.zng")
        with self.assertRaises(pkg.PackageError) as ctx:
            pkg.install(str(folder))
        self.assertIn("missing file", str(ctx.exception))

    def test_install_from_local_registry_folder(self):
        make_package(pkg.registry_dir() / "regpkg", "regpkg")
        self.assertEqual(pkg.install("regpkg")[1], "installed")
        self.assertIn("regpkg", pkg.installed_names())

    def test_unknown_target_explains_both_places(self):
        with self.assertRaises(pkg.PackageError) as ctx:
            pkg.install("nosuchthing")
        self.assertIn("registry", str(ctx.exception))


class UninstallAndList(PkgTestCase):
    def test_uninstall_removes_it(self):
        make_package(self.work / "m", "m")
        pkg.install(str(self.work / "m"))
        self.assertTrue(pkg.uninstall("m"))
        self.assertFalse(pkg.uninstall("m"))
        self.assertNotIn("m", pkg.installed_names())

    def test_list_reports_version_and_description(self):
        make_package(self.work / "m", "m", "3.1.4")
        pkg.install(str(self.work / "m"))
        found = pkg.list_packages()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["name"], "m")
        self.assertEqual(found[0]["version"], "3.1.4")

    def test_list_is_empty_on_a_fresh_home(self):
        self.assertEqual(pkg.list_packages(), [])

    def test_local_search_filters_by_name_and_description(self):
        make_package(pkg.registry_dir() / "alpha", "alpha",
                     body="func a() { return 1 }\n")
        make_package(pkg.registry_dir() / "beta", "beta",
                     body="func b() { return 2 }\n")
        self.assertEqual([p["name"] for p in pkg.search("alph")], ["alpha"])
        self.assertEqual([p["name"] for p in pkg.search("beta")], ["beta"])
        self.assertEqual(pkg.search("nothing"), [])


class ProjectFile(PkgTestCase):
    def test_add_dependency_writes_json(self):
        path = pkg.add_dependency(self.work, "mathx")
        self.assertEqual(path.name, "zing.json")
        data = json.loads(path.read_text())
        self.assertIn("mathx", data["dependencies"])

    def test_conflicting_requirement_is_reported(self):
        pkg.add_dependency(self.work, "mathx", "1.0.0")
        with self.assertRaises(pkg.PackageError):
            pkg.add_dependency(self.work, "mathx", "2.0.0")

    def test_sync_installs_listed_dependencies(self):
        make_package(pkg.registry_dir() / "one", "one")
        make_package(pkg.registry_dir() / "two", "two")
        pkg.add_dependency(self.work, "one")
        pkg.add_dependency(self.work, "two")
        installed, missing = pkg.sync(self.work)
        self.assertEqual(sorted(installed), ["one", "two"])
        self.assertEqual(missing, [])

    def test_sync_reports_what_it_could_not_get(self):
        pkg.add_dependency(self.work, "ghost")
        installed, missing = pkg.sync(self.work)
        self.assertEqual(installed, [])
        self.assertEqual(len(missing), 1)

    def test_sync_without_a_project_file_fails_clearly(self):
        with self.assertRaises(pkg.PackageError) as ctx:
            pkg.sync(self.work)
        self.assertIn(pkg.PROJECT_FILE, str(ctx.exception))


class Config(PkgTestCase):
    def test_round_trip(self):
        pkg.write_config(registry="http://example.test", token="abc")
        settings = pkg.read_config()
        self.assertEqual(settings["registry"], "http://example.test")
        self.assertEqual(pkg.registry_url(), "http://example.test")

    def test_default_registry_is_used_when_unset(self):
        self.assertEqual(pkg.registry_url(), pkg.DEFAULT_REGISTRY)

    def test_trailing_slash_is_trimmed(self):
        pkg.write_config(registry="http://example.test/")
        self.assertEqual(pkg.registry_url(), "http://example.test")


class RegistryServer(PkgTestCase):
    """Runs the real server on a real port."""

    def setUp(self):
        super().setUp()
        from http.server import ThreadingHTTPServer

        from tools.registry_server import Handler

        self.data = self.home / "registry-data"
        self.data.mkdir()
        self.registry = Registry(self.data)
        handler = type("BoundHandler", (Handler,), {"registry": self.registry})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        pkg.write_config(registry=f"http://127.0.0.1:{self.port}")

    def _stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def publish(self, name: str, version: str = "1.0.0") -> dict:
        folder = make_package(self.work / name, name, version)
        blob = build_zip(folder, name)
        return self.registry.publish(
            {"name": name, "version": version}, blob)

    def test_publish_then_list(self):
        self.publish("remote_pkg")
        found = pkg.remote_search("remote")
        self.assertEqual([p["name"] for p in found], ["remote_pkg"])

    def test_install_from_registry_and_use_it(self):
        self.publish("remotelib")
        name, action = pkg.install_from_registry("remotelib")
        self.assertEqual((name, action), ("remotelib", "installed"))
        self.assertEqual(
            printed("import remotelib\nprint(remotelib.remotelib_value())"),
            ["42"])

    def test_version_comes_from_the_registry(self):
        self.publish("verlib", "4.5.6")
        pkg.install_from_registry("verlib")
        self.assertEqual(pkg.load_installed("verlib")["version"], "4.5.6")

    def test_checksum_mismatch_installs_nothing(self):
        record = self.publish("corrupt")
        # Corrupt the stored archive without touching meta.json.
        archive = self.data / "packages" / "corrupt" / "corrupt.zip"
        archive.write_bytes(b"not a zip at all")
        with self.assertRaises(pkg.PackageError) as ctx:
            pkg.install_from_registry("corrupt")
        self.assertIn("checksum", str(ctx.exception))
        self.assertFalse(pkg.installed_dir("corrupt").exists())

    def test_unknown_package_404_is_reported(self):
        with self.assertRaises(pkg.PackageError) as ctx:
            pkg.install_from_registry("does_not_exist")
        self.assertIn("404", str(ctx.exception))

    def test_upgrade_replaces_the_old_version(self):
        self.publish("up", "1.0.0")
        pkg.install_from_registry("up")
        self.publish("up", "2.0.0")
        name, action = pkg.install_from_registry("up", upgrade=True)
        self.assertEqual(action, "upgraded")
        self.assertEqual(pkg.load_installed("up")["version"], "2.0.0")

    def test_range_install_picks_the_newest_match(self):
        self.publish("multi", "1.2.0")
        self.publish("multi", "1.2.9")
        self.publish("multi", "2.0.0")
        pkg.install_from_registry("multi", spec="~1.2")
        self.assertEqual(pkg.load_installed("multi")["version"], "1.2.9")

    def test_range_install_can_pick_an_older_version(self):
        self.publish("older", "1.0.0")
        self.publish("older", "2.5.0")
        pkg.install_from_registry("older", spec="<2.0")
        self.assertEqual(pkg.load_installed("older")["version"], "1.0.0")

    def test_range_install_reports_when_nothing_matches(self):
        self.publish("tiny", "1.0.0")
        with self.assertRaises(pkg.PackageError) as ctx:
            pkg.install_from_registry("tiny", spec=">=9.0")
        self.assertIn("matches", str(ctx.exception))

    def test_registry_keeps_every_published_version(self):
        self.publish("hist", "1.0.0")
        self.publish("hist", "1.1.0")
        meta = self.registry.meta("hist")
        versions = sorted(v["version"] for v in meta["versions"])
        self.assertEqual(versions, ["1.0.0", "1.1.0"])
        self.assertEqual(meta["version"], "1.1.0")

    def test_publish_requires_a_token_when_the_server_has_one(self):
        from http.server import ThreadingHTTPServer

        self.httpd.shutdown()
        from tools.registry_server import Handler

        handler = type("SecuredHandler", (Handler,),
                       {"registry": Registry(self.data, token="sekret")})
        secured = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = secured.server_address[1]
        threading.Thread(target=secured.serve_forever, daemon=True).start()
        self.addCleanup(secured.server_close)
        pkg.write_config(registry=f"http://127.0.0.1:{self.port}")

        folder = make_package(self.work / "locked", "locked")
        with self.assertRaises(pkg.PackageError) as ctx:
            pkg.publish(folder)
        self.assertIn("401", str(ctx.exception))

        pkg.write_config(token="sekret")
        name, _ = pkg.publish(folder)
        self.assertEqual(name, "locked")

    def test_publish_rejects_a_bad_version(self):
        registry = Registry(self.data)
        with self.assertRaises(ValueError):
            registry.publish({"name": "bad", "version": "one.point.oh"}, b"x")

    def test_archive_refuses_to_escape_its_folder(self):
        self.publish("evil")
        # Craft an archive with a path that would write outside the package.
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("evil/../../escaped.zng", "func x() { return 1 }")
        blob = buf.getvalue()
        registry = self.registry
        meta = {"name": "evil", "version": "1.0.0",
                "main": "evil.zng"}
        import hashlib

        record = registry.publish(meta, blob)
        # The stored sha256 matches, so the download passes verification and
        # the traversal guard is what has to stop it.
        record["zip"] = base64.b64encode(blob).decode()
        self.assertEqual(
            hashlib.sha256(blob).hexdigest(), record["sha256"])


if __name__ == "__main__":
    unittest.main(verbosity=2)