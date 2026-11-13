#!/usr/bin/env python3
"""Build the browser extension from reviewed files only."""

import argparse
from pathlib import Path
from tempfile import NamedTemporaryFile
from zipfile import ZIP_STORED, ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "browser-extension"
DESTINATION = ROOT / "browser-extension.zip"
FILES = ("README.md", "manifest.json", "popup.html", "popup.js")


def build(destination: Path) -> None:
    with ZipFile(destination, "w") as archive:
        for name in FILES:
            source = SOURCE / name
            if source.is_symlink() or not source.is_file():
                raise ValueError(f"Not a regular extension source file: {name}")
            member = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            member.create_system = 3
            member.external_attr = 0o100644 << 16
            member.compress_type = ZIP_STORED
            archive.writestr(member, source.read_bytes())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the checked-in ZIP differs")
    args = parser.parse_args()
    if args.check:
        with NamedTemporaryFile() as temp:
            candidate = Path(temp.name)
            build(candidate)
            if not DESTINATION.is_file() or candidate.read_bytes() != DESTINATION.read_bytes():
                parser.exit(1, "browser-extension.zip is stale; run this script without --check\n")
    else:
        build(DESTINATION)


if __name__ == "__main__":
    main()
