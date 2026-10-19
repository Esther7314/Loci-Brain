"""
========================================
package/windows/build.py — build the Windows x64 portable download
========================================

Run on Windows x64 with Python 3.12 (the same minor version as the embedded one: the
dependencies are installed by this interpreter's pip, and jieba only ships as source):

    py -3.12 package/windows/build.py                # -> dist/Loci-<version>-windows-x64.zip
    py -3.12 package/windows/build.py --out D:/tmp   # somewhere else
    py -3.12 package/windows/build.py --no-zip       # leave the folder, skip the zip

What goes in (package/launcher.py describes the layout it expects):
  · python/   the python.org embeddable 3.12, its ._pth opened to Lib/site-packages and
              app/src, the locked dependencies (requirements.lock.txt, hash-checked)
  · node/     node.exe from the official Node 22 LTS zip
  · app/      the runtime files of this repository — no tests, no exam, no buckets
  · 启动.bat and the two start-at-login switches, named for the person who opens the folder

Downloads are checked against the SHA-256 values pinned below and cached in
<out>/.cache, so a second build does not fetch them again.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PYTHON_VERSION = "3.12.10"   # the last 3.12 release python.org built Windows binaries for
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
# The digest in python.org's .sigstore bundle for this file (messageSignature.messageDigest).
PYTHON_SHA256 = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"

NODE_VERSION = "22.23.3"
NODE_URL = f"https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-win-x64.zip"
NODE_SHA256 = "2b0ff57b049cda1bbcea2240eec20467018713c1efe1f7360c2681859b90ed71"  # nodejs.org SHASUMS256.txt

# Repository paths copied into app/. Everything else stays out.
APP_FILES = [
    "src", "frontend", "gateway", "docs", "package/launcher.py",
    "config.default.yaml", "VERSION", "LICENSE", "NOTICE", "README.md", "CHANGELOG.md",
    "scripts/migrate.py",
]
SKIP_DIRS = {"__pycache__", "tests", "data", "node_modules", ".pytest_cache"}

# The files the person sees in the folder. Their contents stay ASCII: cmd reads a .bat in
# the console's code page, and only the file names may be Chinese.
LAUNCH = "启动.bat"
AUTOSTART_ON = "开机自动启动.bat"
AUTOSTART_OFF = "取消开机自动启动.bat"

BAT_START = r"""@echo off
title Loci
cd /d "%~dp0"
"%~dp0python\python.exe" "%~dp0app\package\launcher.py" %*
if errorlevel 1 pause
"""

BAT_AUTOSTART = r"""@echo off
"%~dp0python\python.exe" "%~dp0app\package\launcher.py" --autostart {mode}
pause
"""


def say(line: str) -> None:
    print(f"[build] {line}", flush=True)


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def fetch(url: str, cache: str, expected: str) -> str:
    os.makedirs(cache, exist_ok=True)
    path = os.path.join(cache, url.rsplit("/", 1)[1])
    if not os.path.exists(path):
        say(f"downloading {url}")
        tmp = path + ".part"
        urllib.request.urlretrieve(url, tmp)
        os.replace(tmp, path)
    got = sha256(path)
    if not expected:
        sys.exit(f"[build] no pinned SHA-256 for {url}\n[build] downloaded file hashes to {got} — "
                 "check it against the publisher, pin it in build.py, run again")
    if got != expected:
        os.remove(path)
        sys.exit(f"[build] SHA-256 mismatch for {url}: expected {expected}, got {got}")
    return path


def copy_app(dest: str) -> None:
    def ignore(_dir, names):
        return [n for n in names if n in SKIP_DIRS or n.endswith((".pyc", ".log"))]

    for rel in APP_FILES:
        src = os.path.join(REPO, rel)
        dst = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.isdir(src):
            shutil.copytree(src, dst, ignore=ignore)
        else:
            shutil.copy2(src, dst)


def setup_python(dest: str, cache: str) -> None:
    with zipfile.ZipFile(fetch(PYTHON_URL, cache, PYTHON_SHA256)) as z:
        z.extractall(dest)
    major_minor = "".join(PYTHON_VERSION.split(".")[:2])
    pth = os.path.join(dest, f"python{major_minor}._pth")
    # The embeddable's ._pth replaces sys.path entirely: name the stdlib zip, the site
    # packages, and Loci's src (server.py also adds its own directory, but only once it runs).
    with open(pth, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(f"python{major_minor}.zip\n.\nLib\\site-packages\n..\\app\\src\nimport site\n")
    site = os.path.join(dest, "Lib", "site-packages")
    os.makedirs(site, exist_ok=True)
    say("installing the locked dependencies")
    # --no-compile: the .pyc files are written on first use anyway, and the zip is
    # 90 MB lighter without them.
    subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-warn-script-location",
                    "--no-compile", "--require-hashes", "--target", site,
                    "-r", os.path.join(REPO, "requirements.lock.txt")],
                   check=True)
    prune_tests(site)


# The numeric libraries ship their own test suites inside the package; nothing imports them.
PRUNE_TESTS_IN = ("numpy", "scipy", "sklearn")


def prune_tests(site: str) -> None:
    removed = 0
    for top in PRUNE_TESTS_IN:
        for dirpath, dirnames, _ in os.walk(os.path.join(site, top)):
            for d in [d for d in dirnames if d == "tests"]:
                shutil.rmtree(os.path.join(dirpath, d))
                dirnames.remove(d)
                removed += 1
    say(f"removed {removed} bundled test suites")


def setup_node(dest: str, cache: str) -> None:
    os.makedirs(dest, exist_ok=True)
    inner = f"node-v{NODE_VERSION}-win-x64/node.exe"
    with zipfile.ZipFile(fetch(NODE_URL, cache, NODE_SHA256)) as z:
        with z.open(inner) as src, open(os.path.join(dest, "node.exe"), "wb") as dst:
            shutil.copyfileobj(src, dst)
        for extra in ("LICENSE",):
            with z.open(f"node-v{NODE_VERSION}-win-x64/{extra}") as src, \
                    open(os.path.join(dest, extra), "wb") as dst:
                shutil.copyfileobj(src, dst)


def write_bats(root: str) -> None:
    for name, text in ((LAUNCH, BAT_START),
                       (AUTOSTART_ON, BAT_AUTOSTART.format(mode="on")),
                       (AUTOSTART_OFF, BAT_AUTOSTART.format(mode="off"))):
        with open(os.path.join(root, name), "w", encoding="ascii", newline="\r\n") as f:
            f.write(text)


def main(argv=None) -> int:
    if os.name != "nt" or sys.version_info[:2] != (3, 12):
        sys.exit("[build] run this on Windows with Python 3.12 (py -3.12 package/windows/build.py)")
    p = argparse.ArgumentParser(description="Build the Windows x64 portable download.")
    p.add_argument("--out", default=os.path.join(REPO, "dist"))
    p.add_argument("--no-zip", action="store_true")
    a = p.parse_args(argv)

    with open(os.path.join(REPO, "VERSION"), encoding="utf-8") as f:
        version = f.read().strip()
    name = f"Loci-{version}-windows-x64"
    out = os.path.abspath(a.out)
    root = os.path.join(out, name)
    cache = os.path.join(out, ".cache")
    if os.path.exists(root):
        shutil.rmtree(root)
    os.makedirs(root)

    say(f"building {root}")
    copy_app(os.path.join(root, "app"))
    setup_node(os.path.join(root, "node"), cache)
    setup_python(os.path.join(root, "python"), cache)
    write_bats(root)

    if not a.no_zip:
        archive = shutil.make_archive(os.path.join(out, name), "zip", out, name)
        say(f"wrote {archive} ({os.path.getsize(archive) // (1 << 20)} MB)")
    else:
        say(f"left {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
