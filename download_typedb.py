#!/usr/bin/env python3
"""Download and unpack TypeDB (server + loader) for this platform into run/. Idempotent.

With --typedb-home, use an existing TypeDB distribution instead. If that distribution has
no loader (e.g. a server-only install), the standalone typedb-loader package is
downloaded into run/.
"""

import argparse
import os
import platform
import shutil
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

DEFAULT_VERSION = "3.13.6"
REPOSITORY = "https://repo.typedb.com/public/public-release/raw/names"
ROOT = Path(__file__).resolve().parent
RUN_DIR = ROOT / "run"


def platform_name():
    system = {"Linux": "linux", "Darwin": "mac", "Windows": "windows"}.get(platform.system())
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine().lower())
    if system is None or arch is None:
        sys.exit(f"unsupported platform: {platform.system()} {platform.machine()}")
    return f"{system}-{arch}"


def binary(home, component):
    suffix = ".exe" if platform.system() == "Windows" else ""
    return Path(home) / component / f"typedb_{component}_bin{suffix}"


def download(url, dest):
    if dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    print(f"Downloading {url}", file=sys.stderr)
    try:
        # The package repository rejects urllib's default User-Agent.
        request = urllib.request.Request(url, headers={"User-Agent": "typedb-loading-baseline"})
        with urllib.request.urlopen(request) as response, open(partial, "wb") as out:
            shutil.copyfileobj(response, out)
    except Exception as e:
        partial.unlink(missing_ok=True)
        sys.exit(f"download failed: {url}: {e}")
    partial.rename(dest)


def extract(archive, dest_dir):
    staging = dest_dir.with_name(dest_dir.name + ".extracting")
    shutil.rmtree(staging, ignore_errors=True)
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                path = Path(z.extract(info, staging))
                mode = info.external_attr >> 16
                if mode and not info.is_dir():
                    path.chmod(mode & 0o777)
    else:
        with tarfile.open(archive) as t:
            if hasattr(tarfile, "data_filter"):
                t.extractall(staging, filter="data")
            else:
                t.extractall(staging)
    # Archives contain a single top-level directory named like the package.
    (staging / dest_dir.name).rename(dest_dir)
    staging.rmdir()


def ensure_package(package, version):
    """Download + extract typedb-<package> into run/, returning the extracted directory."""
    name = f"typedb-{package}-{platform_name()}-{version}"
    home = RUN_DIR / name
    if home.is_dir():
        return home
    extension = "tar.gz" if platform_name().startswith("linux") else "zip"
    archive = RUN_DIR / "downloads" / f"{name}.{extension}"
    download(f"{REPOSITORY}/typedb-{package}-{platform_name()}/versions/{version}/{name}.{extension}", archive)
    extract(archive, home)
    return home


def ensure_typedb(version=DEFAULT_VERSION, typedb_home=None):
    """Return (server binary, loader binary)."""
    if typedb_home is None:
        home = ensure_package("all", version)
        return binary(home, "server"), binary(home, "loader")
    server = binary(typedb_home, "server")
    if not server.is_file():
        sys.exit(f"no TypeDB server found at {server}")
    loader = binary(typedb_home, "loader")
    if not loader.is_file():
        print(f"No loader in {typedb_home}; using standalone typedb-loader {version}", file=sys.stderr)
        loader = binary(ensure_package("loader", version), "loader")
    return server, loader


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=DEFAULT_VERSION, help=f"TypeDB version (default: {DEFAULT_VERSION})")
    parser.add_argument("--typedb-home", type=Path, help="use an existing TypeDB distribution directory")
    args = parser.parse_args()
    server, loader = ensure_typedb(args.version, args.typedb_home)
    print(f"server: {server}")
    print(f"loader: {loader}")


if __name__ == "__main__":
    main()
