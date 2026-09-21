#!/usr/bin/env python3
"""Bump the image tag of every compose.yaml under a search root whose
image matches, and set x-spiri-config-version to the same version.

Prints a comma-separated list of changed paths (relative to the search
root) to stdout and exits 0 if anything changed; prints an error to
stderr and exits 1 if nothing matched.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ruamel.yaml import YAML


def bump(search_root: Path, image_ref: str, version: str) -> list[Path]:
    yaml = YAML()
    yaml.preserve_quotes = True

    changed = []
    for path in sorted(search_root.rglob("compose.yaml")):
        with open(path) as f:
            data = yaml.load(f)

        touched = False
        for service in data.get("services", {}).values():
            image = service.get("image", "")
            if image.rsplit(":", 1)[0] == image_ref:
                service["image"] = f"{image_ref}:{version}"
                touched = True

        if not touched:
            continue

        data["x-spiri-config-version"] = version
        with open(path, "w") as f:
            yaml.dump(data, f)
        changed.append(path)

    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--search-root", required=True, type=Path)
    parser.add_argument("--image-ref", required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args(argv)

    version = args.version.lstrip("v")
    changed = bump(args.search_root, args.image_ref, version)

    if not changed:
        print(
            f"No compose.yaml under {args.search_root} references image {args.image_ref!r}",
            file=sys.stderr,
        )
        return 1

    print(",".join(str(p.relative_to(args.search_root)) for p in changed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
