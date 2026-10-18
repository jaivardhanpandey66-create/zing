"""A public registry server for zing packages.

Standard library only -- it runs anywhere Python 3.11 does, with nothing to
install:

    python3 tools/registry_server.py --port 8777 --root ./registry-data

Endpoints
    GET  /api/packages              list every package
    GET  /api/packages/<name>       one package's metadata
    GET  /api/packages/<name>/download   the package as a .zip
    POST /api/packages              publish (needs a token)

Publishing requires a token so strangers cannot overwrite packages. Set one
with --token, or leave it unset to accept anything (fine for local testing,
never do this on a public host).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX_UPLOAD = 10 * 1024 * 1024  # 10 MiB is plenty for a folder of .zng files
NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class Registry:
    """Packages on disk, one folder and one .zip per package."""

    def __init__(self, root: Path, token: str | None = None):
        self.root = root
        self.token = token
        self.lock = threading.Lock()
        (self.root / "packages").mkdir(parents=True, exist_ok=True)

    # -- reading ---------------------------------------------------
    def index(self) -> list[dict]:
        entries = []
        for meta_path in sorted((self.root / "packages").glob("*/meta.json")):
            try:
                entries.append(json.loads(meta_path.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                continue  # skip a corrupt entry rather than break the list
        return entries

    def meta(self, name: str) -> dict | None:
        path = self.root / "packages" / name / "meta.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def archive(self, name: str, version: str | None = None) -> Path | None:
        folder = self.root / "packages" / name
        latest = folder / f"{name}.zip"
        if version:
            meta = self.meta(name)
            # The newest release lives as {name}.zip; older ones are
            # snapshotted under {name}-{version}.zip.
            if meta and str(meta.get("version")) == str(version):
                return latest if latest.is_file() else None
            path = folder / f"{name}-{version}.zip"
            return path if path.is_file() else None
        return latest if latest.is_file() else None

    # -- writing ---------------------------------------------------
    def publish(self, meta: dict, zip_bytes: bytes) -> dict:
        name = str(meta.get("name", ""))
        if not NAME_RE.fullmatch(name):
            raise ValueError(
                "package name must be letters, digits and underscores"
            )
        version = str(meta.get("version", "")).strip()
        if not re.fullmatch(r"\d+(\.\d+)*", version):
            raise ValueError("version must look like 1.2.3")
        digest = hashlib.sha256(zip_bytes).hexdigest()

        with self.lock:
            folder = self.root / "packages" / name
            folder.mkdir(parents=True, exist_ok=True)
            # Keep the previous release so `download` always has something.
            existing = self.meta(name)
            if existing and existing.get("version") != version:
                shutil.copy(
                    folder / f"{name}.zip",
                    folder / f"{name}-{existing['version']}.zip",
                )
            (folder / f"{name}.zip").write_bytes(zip_bytes)
            (folder / f"{name}-{version}.zip").write_bytes(zip_bytes)
            history = []
            if existing:
                history = [h for h in existing.get("versions", [])
                           if isinstance(h, dict)
                           and str(h.get("version")) != version]
            history.append({
                "version": version,
                "sha256": digest,
                "size": len(zip_bytes),
                "file": f"{name}-{version}.zip",
            })
            record = {
                "name": name,
                "version": version,
                "description": str(meta.get("description", "")),
                "requires": list(meta.get("requires", [])),
                "main": str(meta.get("main", "")),
                "sha256": digest,
                "size": len(zip_bytes),
                "versions": history,
            }
            (folder / "meta.json").write_text(
                json.dumps(record, indent=2) + "\n", encoding="utf-8"
            )
            return record


def build_zip(source: Path, name: str) -> bytes:
    """Zip a package folder, keeping it under the name's own top folder."""
    buf = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp = Path(buf.name)
    buf.close()
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(source.rglob("*")):
                if path.is_dir():
                    continue
                if path.suffix in (".pyc",) or "__pycache__" in path.parts:
                    continue
                arcname = str(Path(name) / path.relative_to(source))
                # A fixed timestamp and fixed permissions keep the bytes
                # identical for identical input. Otherwise republishing the
                # same code yields a new sha256 and invalidates every
                # lockfile that pinned the old one.
                info = zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0))
                info.external_attr = 0o644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, path.read_bytes())
        return tmp.read_bytes()
    finally:
        tmp.unlink(missing_ok=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "zing-registry/1.0"
    registry: Registry

    # -- helpers --------------------------------------------------
    def _send_json(self, code: int, payload) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}")

    # -- routes ---------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        path = self.path.split("?", 1)[0].strip("/")
        parts = path.split("/")
        reg = self.registry

        if path in ("", "index.html"):
            self._send_json(200, {"service": "zing registry", "packages": reg.index()})
            return

        if parts[:2] == ["api", "packages"] and len(parts) == 2:
            self._send_json(200, {"packages": reg.index()})
            return

        if parts[:2] == ["api", "packages"] and len(parts) == 3:
            meta = reg.meta(parts[2])
            if meta is None:
                self._send_json(404, {"error": f"no package named {parts[2]!r}"})
            else:
                self._send_json(200, meta)
            return

        if parts[:2] == ["api", "packages"] and len(parts) == 4 \
                and parts[3] == "download":
            from urllib.parse import parse_qs
            query = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path \
                else {}
            version = (query.get("version") or [None])[0]
            archive = reg.archive(parts[2], version)
            if archive is None:
                self._send_json(404, {"error": f"no package named {parts[2]!r}"
                                      + (f" at {version}" if version else "")})
                return
            self._send_bytes(200, archive.read_bytes(), "application/zip")
            return

        self._send_json(404, {"error": "not found", "path": self.path})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0].strip("/") != "api/packages":
            self._send_json(404, {"error": "not found"})
            return
        reg = self.registry
        if reg.token is not None:
            supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
            if supplied != reg.token:
                self._send_json(401, {"error": "bad or missing publish token"})
                return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_UPLOAD:
            self._send_json(400, {"error": "missing or oversized upload"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            record = reg.publish(payload, base64_bytes(payload.get("zip", "")))
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
            return
        except json.JSONDecodeError as e:
            self._send_json(400, {"error": f"bad JSON: {e}"})
            return
        self._send_json(201, record)


def base64_bytes(text: str) -> bytes:
    import base64

    try:
        return base64.b64decode(text, validate=True)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"'zip' must be base64: {e}") from None


def serve(root: Path, port: int, token: str | None, host: str) -> None:
    registry = Registry(root, token)
    handler = type("BoundHandler", (Handler,), {"registry": registry})
    httpd = ThreadingHTTPServer((host, port), handler)
    where = f"http://{'localhost' if host in ('0.0.0.0', '') else host}:{port}"
    print(f"zing registry on {where}")
    print(f"  packages: {len(registry.index())}")
    print(f"  publishing: {'token required' if token else 'OPEN (no token)'}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


def main() -> int:
    ap = argparse.ArgumentParser(description="zing package registry server")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--host", default="127.0.0.1",
                    help="use 0.0.0.0 to accept connections from anywhere")
    ap.add_argument("--root", default="registry-data",
                    help="folder holding published packages")
    ap.add_argument("--token", default=None,
                    help="token clients must send to publish")
    args = ap.parse_args()
    serve(Path(args.root).resolve(), args.port, args.token, args.host)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())