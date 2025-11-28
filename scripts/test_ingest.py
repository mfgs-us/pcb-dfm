from pathlib import Path

from pcb_dfm.ingest import ingest_gerber_zip


def main() -> None:
    zip_path = Path("Gerber.zip")  # replace with an actual file path
    result = ingest_gerber_zip(zip_path)

    print(f"Root dir: {result.root_dir}")
    print(f"Files found: {len(result.files)}")
    for f in result.files:
        print(f"- {f.id}: {f.original_name} -> {f.logical_layer} ({f.layer_type}, side={f.side}, format={f.format})")

    print("\nIssues:")
    for issue in result.issues:
        print(f"- [{issue.severity}] {issue.code}: {issue.message}")


if __name__ == "__main__":
    main()
