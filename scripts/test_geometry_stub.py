from pathlib import Path

from pcb_dfm.ingest import ingest_gerber_zip
from pcb_dfm.geometry import build_board_geometry, queries


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

    # New: board bounds in mm
    bounds = queries.get_board_bounds(geom)
    if bounds:
        w_mm = bounds.max_x - bounds.min_x
        h_mm = bounds.max_y - bounds.min_y
        print(f"\nBoard bounds (mm): "
              f"x={bounds.min_x:.3f}..{bounds.max_x:.3f}, "
              f"y={bounds.min_y:.3f}..{bounds.max_y:.3f}")
        print(f"Board size (mm): {w_mm:.3f} x {h_mm:.3f}")
    else:
        print("\nNo board bounds available (no polygons).")


if __name__ == "__main__":
    main()
