#!/usr/bin/env python3
import json
from pathlib import Path

SCHEMA_VERSION = "1.0.0"


def main() -> None:
    # Resolve repo root as parent of this script directory
    script_path = Path(__file__).resolve()
    repo_root = script_path.parent.parent
    checks_dir = repo_root / "checks"
    index_path = checks_dir / "index.json"

    if not checks_dir.is_dir():
        raise SystemExit(f"Expected 'checks' directory at {checks_dir}, but it does not exist.")

    checks = []

    for path in sorted(checks_dir.glob("*.json")):
        # Skip the index file itself if it already exists
        if path.name == "index.json":
            continue

        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            raise SystemExit(f"Failed to parse {path}: {e}")

        check_id = data.get("id")
        category_id = data.get("category_id")

        if not check_id:
            raise SystemExit(f"File {path} is missing required field 'id'.")
        if not category_id:
            raise SystemExit(f"File {path} is missing required field 'category_id'.")

        checks.append(
            {
                "id": check_id,
                "category_id": category_id,
                "filename": path.name,
            }
        )

    # Sort checks by category then id for stable diffs
    checks.sort(key=lambda c: (c["category_id"], c["id"]))

    index = {
        "schema_version": SCHEMA_VERSION,
        "generated": "scripts/build_index.py",
        "checks": checks,
    }

    # Write index.json
    with index_path.open("w", encoding="utf-8") as f:
        json.dump(index, f, indent=2)
        f.write("\n")

    print(f"Wrote {index_path} with {len(checks)} checks.")


if __name__ == "__main__":
    main()
