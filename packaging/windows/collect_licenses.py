"""Collect installed Python distribution license notices for the Windows ZIP."""
from __future__ import annotations

import argparse
import csv
from importlib import metadata
from pathlib import Path
import re
import shutil


LICENSE_PREFIXES = ("license", "copying", "notice", "authors")


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "package"


def collect(destination: Path) -> int:
    destination.mkdir(parents=True, exist_ok=True)
    rows = []
    distributions = sorted(
        metadata.distributions(),
        key=lambda distribution: (distribution.metadata.get("Name") or "").casefold(),
    )
    for distribution in distributions:
        name = distribution.metadata.get("Name") or "unknown"
        version = distribution.version or "unknown"
        expression = (
            distribution.metadata.get("License-Expression")
            or distribution.metadata.get("License")
            or "UNKNOWN"
        ).strip()
        package_dir = destination / safe_name(f"{name}-{version}")
        copied = 0
        for relative in distribution.files or ():
            relative_path = Path(str(relative))
            if not relative_path.name.casefold().startswith(LICENSE_PREFIXES):
                continue
            source = Path(distribution.locate_file(relative))
            if not source.is_file() or source.is_symlink():
                continue
            package_dir.mkdir(parents=True, exist_ok=True)
            target = package_dir / safe_name(relative_path.name)
            if target.exists():
                target = package_dir / f"{copied}-{safe_name(relative_path.name)}"
            shutil.copyfile(source, target)
            copied += 1
        rows.append((name, version, expression, copied))

    with (destination / "PACKAGES.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.writer(handle)
        writer.writerow(("name", "version", "declared_license", "license_files"))
        writer.writerows(rows)
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    count = collect(args.destination)
    print(f"Collected license metadata for {count} installed distributions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
