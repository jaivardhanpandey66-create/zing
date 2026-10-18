"""Package manager for zing.

A package is an ordinary folder holding .zng files plus a `zing.json`
manifest:

    {
      "name": "mathx",
      "version": "0.1.0",
      "description": "extra maths helpers",
      "main": "mathx.zng",
      "requires": ["jsonpack >=0.1"]
    }

Packages install into ~/.zing/packages/<name>/ and are then importable by
name from any program:

    import mathx
    print(mathx.mean([1, 2, 3]))

There is no network registry yet -- `search` looks in the local registry
folder ~/.zing/registry/, and `install` also takes a path. This keeps every
command honest: nothing pretends to reach a server that does not exist.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

MANIFEST = "zing.json"


class PackageError(Exception):
    """A problem the user can fix: bad manifest, missing file, bad name."""


def home() -> Path:
    """The zing home folder, honouring ZING_HOME for tests and sandboxes."""
    import os

    return Path(os.environ.get("ZING_HOME", Path.home() / ".zing"))


def packages_dir() -> Path:
    return home() / "packages"


def registry_dir() -> Path:
    return home() / "registry"


def installed_dir(name: str) -> Path:
    return packages_dir() / name


def installed_names() -> list[str]:
    root = packages_dir()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def _check_name(name: str) -> str:
    """Package names are identifiers, so they can be imported directly."""
    if not name:
        raise PackageError("a package needs a name")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise PackageError(
            f"{name!r} is not a valid package name: use letters, digits "
            f"and underscores, and do not start with a digit"
        )
    return name


def read_manifest(folder: Path) -> dict:
    path = folder / MANIFEST
    if not path.is_file():
        raise PackageError(f"{folder} has no {MANIFEST}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise PackageError(f"{path} is not valid JSON: {e}") from None
    if not isinstance(data, dict):
        raise PackageError(f"{path} must contain a JSON object")
    if not data.get("name"):
        raise PackageError(f'{path} needs a "name" field')
    _check_name(str(data["name"]))
    return data


def load_installed(name: str) -> dict | None:
    """The manifest of an installed package, or None if it is not there."""
    folder = installed_dir(name)
    if not (folder / MANIFEST).is_file():
        return None
    try:
        return read_manifest(folder)
    except PackageError:
        return None


def find_entry(name: str, manifest: dict) -> str:
    """The file `import <name>` should load.

    The manifest's "main" wins, but a same-named .zng file is accepted too so
    a one-file package does not need a manifest to be importable.
    """
    main = manifest.get("main")
    if main:
        candidate = installed_dir(name) / str(main)
        if not candidate.is_file():
            raise PackageError(
                f"package {name!r} points at a missing file: {main}"
            )
        return str(candidate)
    default = installed_dir(name) / f"{name}.zng"
    if default.is_file():
        return str(default)
    raise PackageError(
        f"package {name!r} has no {MANIFEST} \"main\" entry and no "
        f"{name}.zng file"
    )


def _parse_version(text: str) -> tuple[int, ...]:
    parts = []
    for chunk in str(text).split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _cmp_versions(a: str, b: str) -> int:
    """Compare two versions, padding the shorter one with zeros."""
    av, bv = _parse_version(a), _parse_version(b)
    length = max(len(av), len(bv))
    av = av + (0,) * (length - len(av))
    bv = bv + (0,) * (length - len(bv))
    return (av > bv) - (av < bv)


_OP_RE = re.compile(r"^(>=|<=|==|!=|>|<|~|\^)\s*([\d.]+)$")
_WILD_RE = re.compile(r"^([\d.]+)\.(?:x|X|\*)$")


def _tilde(version: str, want: str) -> bool:
    """~1.2 means >=1.2 and <2.0; ~1.2.3 means >=1.2.3 and <1.3.0."""
    if _cmp_versions(version, want) < 0:
        return False
    parts = _parse_version(want)
    upper = (parts[0], parts[1] + 1, 0) if len(parts) >= 3 \
        else (parts[0] + 1, 0)
    return _cmp_versions(version, ".".join(map(str, upper))) < 0


def _caret(version: str, want: str) -> bool:
    """^1.2.3 means >=1.2.3 and <2.0.0 (next minor while the major is 0)."""
    if _cmp_versions(version, want) < 0:
        return False
    parts = _parse_version(want)
    if parts and parts[0] > 0:
        upper = (parts[0] + 1, 0, 0)
    else:
        upper = (0, (parts[1] + 1 if len(parts) > 1 else 1), 0)
    return _cmp_versions(version, ".".join(map(str, upper))) < 0


def _match_one(version: str, clause: str) -> bool:
    clause = clause.strip()
    if clause in ("", "*", "latest"):
        return True
    m = _OP_RE.match(clause)
    if m:
        op, want = m.group(1), m.group(2)
        c = _cmp_versions(version, want)
        if op == ">=":
            return c >= 0
        if op == "<=":
            return c <= 0
        if op == "==":
            return c == 0
        if op == "!=":
            return c != 0
        if op == ">":
            return c > 0
        if op == "<":
            return c < 0
        if op == "~":
            return _tilde(version, want)
        return _caret(version, want)
    m = _WILD_RE.match(clause)
    if m:
        want = _parse_version(m.group(1))
        return _parse_version(version)[:len(want)] == want
    if re.fullmatch(r"[\d.]+", clause):
        # A bare version means "at least this one".
        return _cmp_versions(version, clause) >= 0
    raise PackageError(f"cannot read version rule {clause!r}")


def version_matches(version: str, spec: str) -> bool:
    """Does a version satisfy a rule like '>=1.0, <2.0', '~1.2' or '1.2.*'?"""
    if spec is None:
        return True
    spec = str(spec).strip()
    if spec in ("", "*"):
        return True
    return all(_match_one(version, clause) for clause in spec.split(","))


def resolve_best(versions, spec: str) -> str | None:
    """The highest version in a list that satisfies spec, or None."""
    ok = [str(v) for v in versions if version_matches(str(v), spec)]
    if not ok:
        return None
    return max(ok, key=_parse_version)


def parse_requirement(text: str) -> tuple[str, str]:
    """Split 'mathx >=0.2' into ('mathx', '>=0.2').

    Accepts comparisons, ~ and ^ ranges, wildcards like 1.2.*, and
    comma-separated ranges such as '>=1.0, <2.0'.
    """
    match = re.fullmatch(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*(.*?)\s*", text)
    if not match:
        raise PackageError(f'cannot read requirement {text!r}: try "name >=1.0"')
    name, spec = match.groups()
    return name, (spec or ">=0")


def requirement_met(text: str) -> bool:
    """Is the installed package a version the requirement allows?"""
    name, spec = parse_requirement(text)
    manifest = load_installed(name)
    if manifest is None:
        return False
    return version_matches(str(manifest.get("version", "0")), spec)


def _copy_package(source: Path, dest: Path) -> None:
    shutil.copytree(
        source,
        dest,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"),
    )


def install(source: str, *, upgrade: bool = False) -> tuple[str, str]:
    """Install a package from a folder path or the local registry.

    Returns (name, action) where action is "installed", "upgraded" or
    "unchanged".
    """
    src = Path(source).expanduser()
    if not src.exists():
        candidate = registry_dir() / source
        if candidate.is_dir():
            src = candidate
        elif (registry_dir() / source / MANIFEST).is_file():
            src = registry_dir() / source
    if not src.is_dir():
        raise PackageError(
            f"cannot find package {source!r}. Give a folder path, or put the "
            f"package in {registry_dir()}"
        )

    manifest = read_manifest(src)
    name = _check_name(str(manifest["name"]))

    missing = [
        req
        for req in manifest.get("requires", [])
        if not requirement_met(req)
    ]
    if missing:
        listed = ", ".join(missing)
        raise PackageError(
            f"{name} needs these packages installed first: {listed}"
        )

    dest = installed_dir(name)
    if dest.exists():
        if not upgrade:
            have = load_installed(name) or {}
            same = have.get("version") == manifest.get("version")
            if same:
                return name, "unchanged"
            raise PackageError(
                f"{name} {manifest.get('version')} is already installed "
                f"(you have {have.get('version')}). Use `install {source} "
                f"--upgrade` to replace it."
            )
        shutil.rmtree(dest)

    dest.parent.mkdir(parents=True, exist_ok=True)
    _copy_package(src, dest)

    # A manifest can name a main file that does not exist; catch that now
    # rather than at import time.
    find_entry(name, manifest)
    return name, "upgraded" if upgrade and dest.exists() else "installed"


def uninstall(name: str) -> bool:
    _check_name(name)
    folder = installed_dir(name)
    if not folder.is_dir():
        return False
    shutil.rmtree(folder)
    return True


def list_packages() -> list[dict]:
    found = []
    for name in installed_names():
        manifest = load_installed(name) or {"name": name}
        found.append(manifest)
    return found


def registry_packages() -> list[str]:
    root = registry_dir()
    if not root.is_dir():
        return []
    return sorted(
        p.name for p in root.iterdir() if p.is_dir() and (p / MANIFEST).is_file()
    )


def search(term: str = "") -> list[dict]:
    needle = term.lower()
    found = []
    for name in registry_packages():
        folder = registry_dir() / name
        try:
            manifest = read_manifest(folder)
        except PackageError:
            continue
        blob = f"{manifest.get('name','')} {manifest.get('description','')}".lower()
        if needle in blob:
            manifest["installed"] = load_installed(str(manifest["name"])) is not None
            found.append(manifest)
    return found


def package_path(name: str) -> str | None:
    """The entry file for an installed package, or None if not installed."""
    manifest = load_installed(name)
    if manifest is None:
        return None
    try:
        return find_entry(name, manifest)
    except PackageError:
        return None


# ------------------------------------------------------------- project file

PROJECT_FILE = "zing.json"


def read_project(folder: Path) -> dict | None:
    path = folder / PROJECT_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise PackageError(f"{path} is not valid JSON: {e}") from None
    if not isinstance(data, dict):
        raise PackageError(f"{path} must contain a JSON object")
    return data


def write_project(folder: Path, data: dict) -> Path:
    path = folder / PROJECT_FILE
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def add_dependency(folder: Path, name: str, version: str = "*") -> Path:
    """Record a dependency in the project's zing.json."""
    project = read_project(folder) or {"name": folder.name, "version": "0.1.0"}
    deps = project.setdefault("dependencies", {})
    if name in deps and deps[name] != version:
        raise PackageError(
            f"{name} is already listed as {deps[name]} in {folder / PROJECT_FILE}"
        )
    deps[name] = version
    return write_project(folder, project)


def sync(folder: Path) -> tuple[list[str], list[str]]:
    """Install everything the project lists. Returns (installed, missing)."""
    project = read_project(folder)
    if project is None:
        raise PackageError(f"no {PROJECT_FILE} in {folder}")
    installed, missing = [], []
    for name, want in (project.get("dependencies") or {}).items():
        spec = want if want not in (None, "") else "*"
        try:
            # A local folder or the local registry wins, so a package being
            # developed here shadows the published one. Only if there is no
            # local copy do we go to the network.
            try:
                install(name, upgrade=True)
            except PackageError:
                install_from_registry(name, upgrade=True, spec=spec)
        except PackageError:
            missing.append(f"{name} {want}".strip())
            continue
        if not requirement_met(f"{name} {spec}"):
            missing.append(f"{name} {want}".strip())
            continue
        installed.append(name)
    return installed, missing

# ------------------------------------------------------------------ remote

DEFAULT_REGISTRY = "https://zing.zing.dev"


def config_path():
    return home() / "config.json"


def registry_settings(overrides: dict | None = None) -> dict:
    """Merge saved configuration with command-line overrides.

    An override always wins, so a script can publish to a different registry
    without editing the config file.
    """
    settings = read_config()
    settings.setdefault("registry", DEFAULT_REGISTRY)
    settings.setdefault("token", "")
    for key, value in (overrides or {}).items():
        if value in (None, ""):
            continue
        if key in ("registry", "token"):
            settings[key] = value
    return settings


def read_config() -> dict:
    path = config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise PackageError(f"{path} is not valid JSON: {e}") from None
    return data if isinstance(data, dict) else {}


def write_config(**settings) -> Path:
    data = read_config()
    data.update(settings)
    home().mkdir(parents=True, exist_ok=True)
    path = config_path()
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def registry_url() -> str:
    return str(read_config().get("registry") or DEFAULT_REGISTRY).rstrip("/")


def _resolve(registry: str = None, token: str = None) -> tuple[str, str]:
    """Work out which registry and token to use for one request.

    Command-line values win over the config file, so a script can talk to a
    throwaway registry without disturbing the saved settings.
    """
    settings = registry_settings(
        {"registry": registry, "token": token} if (registry or token) else None)
    base = settings["registry"].rstrip("/")
    return base, settings["token"]


def _request(path: str, payload: dict | None = None, timeout: float = 20.0,
             registry: str = None, token: str = None):
    """Talk to the registry. Raises PackageError with a readable message."""
    import urllib.error
    import urllib.request

    base_url, saved_token = _resolve(registry, token)
    url = base_url + path
    data = None
    headers = {"Accept": "application/json", "User-Agent": "zing-pkg"}
    if payload is not None:
        import base64

        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
        if saved_token:
            headers["Authorization"] = f"Bearer {saved_token}"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8")).get("error", "")
        except Exception:  # noqa: BLE001
            pass
        raise PackageError(
            f"registry said {e.code} {e.reason}{': ' + detail if detail else ''}"
        ) from None
    except urllib.error.URLError as e:
        raise PackageError(
            f"cannot reach the registry at {url}: {e.reason}\n"
            f"check your connection, or point elsewhere with:\n"
            f"  zing config registry <url>"
        ) from None
    except TimeoutError:
        raise PackageError(f"the registry at {url} timed out") from None
    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        raise PackageError(f"the registry sent something that is not JSON") from None


def _request_bytes(path: str, timeout: float = 60.0,
                   registry: str = None, token: str = None) -> bytes:
    """Fetch a non-JSON response, such as a package archive."""
    import urllib.error
    import urllib.request

    base_url, _ = _resolve(registry, token)
    url = base_url + path
    req = urllib.request.Request(
        url, headers={"User-Agent": "zing-pkg", "Accept": "application/zip"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise PackageError(f"registry said {e.code} {e.reason}") from None
    except urllib.error.URLError as e:
        raise PackageError(
            f"cannot reach the registry at {url}: {e.reason}"
        ) from None
    except TimeoutError:
        raise PackageError(f"the registry at {url} timed out") from None


def remote_search(term: str = "", registry: str = None,
                   token: str = None) -> list[dict]:
    packages = _request("/api/packages", registry=registry,
                       token=token).get("packages", [])
    needle = term.lower()
    found = []
    for meta in packages:
        blob = f"{meta.get('name','')} {meta.get('description','')}".lower()
        if needle and needle not in blob:
            continue
        meta = dict(meta)
        meta["installed"] = load_installed(str(meta.get("name", ""))) is not None
        found.append(meta)
    return found


def install_from_registry(name: str, *, upgrade: bool = False,
                        registry: str = None, token: str = None,
                        spec: str = None):
    """Download a package from the configured registry and install it.

    spec is a version range such as '~1.2' or '>=1.0, <2.0'. When given,
    the newest published version that satisfies it is chosen.
    """
    import hashlib
    import io
    import zipfile

    _check_name(name)
    if installed_dir(name).exists() and not upgrade:
        have = load_installed(name) or {}
        raise PackageError(
            f"{name} {have.get('version')} is already installed. "
            f"Use `install {name} --upgrade` to update it."
        )

    info = _request(f"/api/packages/{name}", registry=registry, token=token)
    history = [h for h in (info.get("versions") or []) if isinstance(h, dict)]
    # Pick the newest release that satisfies the requested range, if any.
    if spec is not None and str(spec).strip() not in ("", "*", "latest"):
        available = [str(h.get("version")) for h in history] \
            or [str(info.get("version", "0"))]
        best = resolve_best(available, str(spec))
        if best is None:
            raise PackageError(
                f"no published version of {name} matches {str(spec).strip()!r} "
                f"(available: {', '.join(available)})")
        version = best
    else:
        version = str(info.get("version", "0"))
    entry = next(
        (h for h in history if str(h.get("version")) == version), None)
    query = f"?version={version}" if version else ""
    blob = _request_bytes(f"/api/packages/{name}/download{query}",
                           registry=registry, token=token)

    expected = (entry or {}).get("sha256") or info.get("sha256")
    if expected:
        actual = hashlib.sha256(blob).hexdigest()
        if actual != expected:
            raise PackageError(
                f"{name} {version} failed its checksum: the download does not "
                f"match what the registry published. Nothing was installed."
            )

    dest = installed_dir(name)
    if dest.exists():
        shutil.rmtree(dest)
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            _safe_extract(zf, dest)
    except zipfile.BadZipFile:
        raise PackageError(f"{name} download is not a readable zip file") from None

    # The published archive wraps everything in a folder named after the
    # package; flatten it so imports line up with local packages.
    nested = dest / name
    if nested.is_dir() and not (dest / MANIFEST).exists():
        shutil.move(str(nested), str(dest.parent / f".{name}.staging"))
        shutil.rmtree(dest)
        shutil.move(str(dest.parent / f".{name}.staging"), str(dest))

    manifest = read_manifest(dest)
    manifest["version"] = version
    # Keep the archive hash so the install can be re-verified later without
    # trusting the registry again, and so the lockfile can pin it.
    manifest["sha256"] = expected or hashlib.sha256(blob).hexdigest()
    (dest / MANIFEST).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    find_entry(name, manifest)
    return name, "upgraded" if upgrade else "installed"


def publish(folder: Path, registry: str = None, token: str = None):
    """Upload a package folder to the configured registry."""
    import base64
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from tools.registry_server import build_zip  # type: ignore

    folder = Path(folder).resolve()
    manifest = read_manifest(folder)
    name = _check_name(str(manifest["name"]))
    if "version" not in manifest:
        raise PackageError(f'{folder / MANIFEST} needs a "version" field')
    zip_bytes = build_zip(folder, name)
    payload = dict(manifest)
    payload["zip"] = base64.b64encode(zip_bytes).decode("ascii")
    record = _request("/api/packages", registry=registry, token=token, payload=payload, timeout=60.0)
    return name, record


# ---------------------------------------------------------------- the lockfile

LOCK_FILE = "zing.lock"


def write_lock(folder: Path) -> Path:
    """Record exactly what is installed, so a project can be reproduced.

    A lockfile pins versions and content hashes. Without it, `sync` installs
    whatever the registry publishes today, which can differ tomorrow.
    """
    entries = []
    for name in installed_names():
        manifest = load_installed(name) or {"name": name}
        digest = ""
        main = manifest.get("main") or f"{name}.zng"
        target = installed_dir(name) / str(main)
        if target.is_file():
            import hashlib

            digest = hashlib.sha256(target.read_bytes()).hexdigest()
        entries.append({
            "name": name,
            "version": str(manifest.get("version", "0")),
            "sha256": digest,
        })
    entries.sort(key=lambda e: e["name"])
    path = folder / LOCK_FILE
    path.write_text(
        json.dumps({"lockfile": 1, "packages": entries}, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def read_lock(folder: Path) -> list[dict]:
    path = folder / LOCK_FILE
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise PackageError(f"{path} is not valid JSON: {e}") from None
    packages = data.get("packages")
    return packages if isinstance(packages, list) else []


def _safe_extract(zf, dest: Path) -> None:
    """Extract, refusing any member that would land outside dest.

    The server's checksum only proves the archive is what was published; it
    cannot prove the published archive is harmless, so the paths are checked
    here as well.
    """
    root = dest.resolve()
    for member in zf.namelist():
        target = (root / member).resolve()
        if target != root and root not in target.parents:
            raise PackageError(
                f"package contains a file outside its folder: {member}"
            )
    zf.extractall(root)


def drop_dependency(folder: Path, name: str) -> bool:
    """Remove a package from the project file. True if it was listed."""
    project = read_project(folder)
    if not project:
        return False
    deps = project.get("dependencies")
    if not isinstance(deps, dict) or name not in deps:
        return False
    del deps[name]
    write_project(folder, project)
    return True
