"""
Nuitka build script for QSOCapture.

Builds a standalone Windows EXE with an embedded Qt WebEngine browser
(PySide6 / QWebEngineView). No external browser or Python required.

Usage:
    python build_nuitka.py                  # onedir (folder for installer)
    python build_nuitka.py --onefile        # single-file portable EXE
"""
import argparse
import os
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description="Build QSOCapture with Nuitka")
    parser.add_argument(
        "--onefile",
        action="store_true",
        help="Build a single-file portable EXE (default: onedir folder)",
    )
    args = parser.parse_args()

    app_version = os.environ.get("APP_VERSION", "")
    version_suffix = f"-{app_version}" if app_version else ""

    # The main entry point
    main_script = "qt_launcher.py"

    # Data files to bundle
    data_files = [
        "index.html",
        "icon.svg",
        "icon.ico",
    ]

    # Base Nuitka command
    cmd = [
        sys.executable,
        "-m",
        "nuitka",
        "--standalone" if not args.onefile else "--onefile",
        # Enable PySide6 plugin for automatic Qt DLL handling
        "--enable-plugin=pyside6",
        # No console window for a desktop app
        "--windows-console-mode=disable",
        # Application icon
        f"--windows-icon-from-ico=icon.ico",
        # Auto-accept downloads (Dependency Walker, etc.)
        "--assume-yes-for-downloads",
        # Link-time optimisation — produces smaller, more optimised binaries
        # which are less likely to trigger heuristic antivirus detection.
        "--lto=yes",
        # Include a proper UAC manifest so Windows recognises the app as a
        # well-behaved desktop application rather than an unknown binary.
        "--windows-uac-uiaccess",
        # Disable ccache to ensure fully reproducible builds (ccache can
        # sometimes produce non-deterministic output that looks suspicious).
        "--disable-ccache",
        # Strip docstrings to reduce binary size (smaller EXE is less likely
        # to trigger heuristic detection).
        "--python-flag=-OO",
        # Remove temporary build artefacts after compilation.
        "--remove-output",
        # Windows metadata (VERSIONINFO) — makes the EXE look like a proper
        # signed application rather than an unknown binary.
        "--windows-company-name=SQ3RX",
        "--windows-product-name=QSOCapture",
        "--windows-file-description=Amateur Radio Contest Audio Recorder",
        # Include data files
    ]

    for f in data_files:
        cmd.append(f"--include-data-files={f}={f}")

    # Explicitly include modules that may not be auto-detected
    hidden_modules = [
        "main",
        "config",
        "db",
        "audio_manager",
        "n1mm_listener",
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
    ]
    for mod in hidden_modules:
        cmd.append(f"--include-module={mod}")

    # Output filename (only meaningful for --onefile; for standalone Nuitka
    # always names the .dist folder after the main script, qt_launcher)
    if args.onefile:
        exe_name = f"QSOCapture-portable{version_suffix}"
    else:
        exe_name = "QSOCapture"
    cmd.append(f"--output-filename={exe_name}.exe")

    # Add the main script
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