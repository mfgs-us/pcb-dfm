from pathlib import Path

from pcb_dfm.ingest import ingest_gerber_zip
from pcb_dfm.geometry import build_board_geometry


def main() -> None:
    zip_path = Path("Gerber.zip")  # adjust as needed
    ingest_result = ingest_gerber_zip(zip_path)
    geom = build_board_geometry(ingest_result)

    print(f"Root dir: {geom.root_dir}")
    print(f"Layers: {len(geom.layers)}")
    for layer in geom.layers:
        print(
            f"- {layer.name} "
            f"(logical={layer.logical_layer}, type={layer.layer_type}, side={layer.side}) "
            f"files={len(layer.files)}, polygons={len(layer.polygons)}"
        )


if __name__ == "__main__":
    main()
