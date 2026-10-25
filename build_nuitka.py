"""Nuitka build script for QSOCapture.

Builds a standalone Windows EXE with embedded Qt WebEngine (PySide6).
Produces a onedir folder (qt_launcher.dist/) for Inno Setup, or --onefile
for a portable EXE.

Usage:
    python build_nuitka.py                  # onedir (for installer)
    python build_nuitka.py --onefile        # single-file portable EXE
"""

import argparse
import os
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description="Build QSOCapture with Nuitka")
    parser.add_argument(
        "--onefile", action="store_true",
        help="Build a single-file portable EXE (default: onedir folder for installer)",
    )
    args = parser.parse_args()

    app_version = os.environ.get("APP_VERSION", "")

    import re

    from version import APP_VERSION as DEFAULT_APP_VERSION
    if not app_version:
        app_version = DEFAULT_APP_VERSION
    version_suffix = f"-{app_version}" if app_version else ""

    numeric_version = re.sub(r"[^0-9.]", "", app_version) or DEFAULT_APP_VERSION
    # Nuitka requires each version part to be a 16-bit number (0-65535).
    # Manual builds use a timestamp (e.g. 20261020.191530) in the installer
    # filename, but the EXE metadata should still carry a sane release version.
    parts = [p for p in numeric_version.split(".") if p != ""]
    if any(int(p) > 65535 for p in parts):
        numeric_version = re.sub(r"[^0-9.]", "", DEFAULT_APP_VERSION) or "0.0.0"

    main_script = "qt_launcher.py"
    data_files = ["index.html", "icon.svg", "icon.ico"]

    cmd = [
        sys.executable, "-m", "nuitka",
        "--standalone" if not args.onefile else "--onefile",
        "--enable-plugin=pyside6",
        "--windows-console-mode=disable",
        "--windows-icon-from-ico=icon.ico",
        "--assume-yes-for-downloads",
        "--lto=yes",
        "--disable-ccache",
        "--python-flag=-OO",
        "--remove-output",
        "--windows-company-name=SQ3RX",
        "--windows-product-name=QSOCapture",
        "--windows-file-description=Amateur Radio Contest Audio Recorder",
        f"--windows-file-version={numeric_version}",
        f"--windows-product-version={numeric_version}",
    ]

    for f in data_files:
        cmd.append(f"--include-data-files={f}={f}")

    hidden_modules = [
        "main", "config", "db", "audio_manager", "n1mm_listener",
        "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
        "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan", "uvicorn.lifespan.on",
    ]
    for mod in hidden_modules:
        cmd.append(f"--include-module={mod}")

    # websockets resolves its public API (e.g. websockets.connect) lazily via
    # lazy_import, so Nuitka's static analysis misses the websockets.asyncio
    # subpackage and the TCI client fails with "No module named
    # 'websockets.asyncio'" in the packaged build. Include the whole package.
    hidden_packages = ["websockets"]
    for pkg in hidden_packages:
        cmd.append(f"--include-package={pkg}")

    if args.onefile:
        exe_name = f"QSOCapture-portable{version_suffix}"
    else:
        exe_name = "QSOCapture"
    cmd.append(f"--output-filename={exe_name}.exe")
    cmd.append(main_script)

    print(f"Running: {' '.join(cmd)}")
    sys.stdout.flush()

    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        print(f"ERROR: Nuitka build failed with exit code {result.returncode}")
        sys.exit(1)
    print("Build completed successfully!")


if __name__ == "__main__":
    main()
