"""py2app build: pip install -e .. (repo root) then: cd macos_app && python setup.py py2app"""

from __future__ import annotations

from pathlib import Path

from setuptools import find_packages, setup

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SRC = REPO / "src"

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
    "packages": ["zaliver", "PyQt6", "numpy"],
}

setup(
    name="zaliver-py2app",
    version="0.1.0",
    packages=find_packages(where=str(SRC)),
    package_dir={"": str(SRC)},
    package_data={"zaliver.ui": ["theme.qss"]},
    include_package_data=True,
    app=[str(HERE / "Zaliver.py")],
    options={"py2app": OPTIONS},
)
