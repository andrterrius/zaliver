"""py2app build for macOS: python setup_py2app.py py2app"""

from __future__ import annotations

from pathlib import Path

from setuptools import find_packages, setup

ROOT = Path(__file__).resolve().parent

OPTIONS = {
    "argv_emulation": True,
    "plist": {
        "CFBundleName": "Zaliver",
        "CFBundleDisplayName": "Zaliver",
        "CFBundleIdentifier": "com.zaliver.desktop",
        "CFBundleVersion": "0.1.0",
        "CFBundleShortVersionString": "0.1.0",
        "NSHighResolutionCapable": True,
    },
    "packages": ["zaliver", "PyQt6", "numpy", "cv2"],
}

setup(
    name="zaliver-py2app",
    version="0.1.0",
    packages=find_packages(where=str(ROOT / "src")),
    package_dir={"": str(ROOT / "src")},
    package_data={"zaliver.ui": ["theme.qss"]},
    include_package_data=True,
    app=[str(ROOT / "Zaliver.py")],
    options={"py2app": OPTIONS},
)
