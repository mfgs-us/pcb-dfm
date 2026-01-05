#!/usr/bin/env python3
"""
Debug script to investigate coordinate system issues.
"""

import sys
from pathlib import Path

# Add the pcb_dfm package to the path
sys.path.insert(0, str(Path(__file__).parent))

try:
    from pcb_dfm.geometry.gerber_parser import build_board_geometry, _inch_to_mm
    from pcb_dfm.geometry.queries import get_board_bounds
    from pcb_dfm.ingest import ingest_gerber_zip
    from pcb_dfm.geometry.primitives import Point2D
    import gerber
except ImportError as e:
    print(f"Import error: {e}")
    print("Make sure the pcb_dfm package is available")
    sys.exit(1)

def debug_coordinate_system(zip_path):
    """Debug coordinate system by examining actual parsed coordinates."""
    print(f"Debugging coordinate system for: {zip_path}")
    
    # Ingest the gerber files
    try:
        ingest_result = ingest_gerber_zip(Path(zip_path))
        print(f"Ingested {len(ingest_result.files)} files")
        
        for f in ingest_result.files:
            print(f"  - {f.original_name} -> {f.logical_layer} ({f.layer_type})")
    except Exception as e:
        print(f"Ingest failed: {e}")
        return
    
    # Build geometry
    try:
        geom = build_board_geometry(ingest_result)
        print(f"Built geometry with {len(geom.layers)} layers")
    except Exception as e:
        print(f"Geometry building failed: {e}")
        return
    
    # Get board bounds
    bounds = get_board_bounds(geom)
    if bounds:
        print(f"\nBoard bounds:")
        print(f"  X: {bounds.min_x:.3f} .. {bounds.max_x:.3f} (width: {bounds.max_x - bounds.min_x:.3f})")
        print(f"  Y: {bounds.min_y:.3f} .. {bounds.max_y:.3f} (height: {bounds.max_y - bounds.min_y:.3f})")
    else:
        print("\nNo board bounds found")
    
    # Examine coordinates in each layer
    print(f"\nLayer coordinate details:")
    for layer in geom.layers:
        if not layer.polygons:
            continue
            
        print(f"\n{layer.logical_layer} ({layer.layer_type}): {len(layer.polygons)} polygons")
        
        # Show first few polygons with their coordinate ranges
        for i, poly in enumerate(layer.polygons[:3]):
            b = poly.bounds()
            print(f"  Poly {i}: X={b.min_x:.3f}..{b.max_x:.3f}, Y={b.min_y:.3f}..{b.max_y:.3f}")
            
            # Show first few vertices
            if hasattr(poly, 'vertices') and poly.vertices:
                print(f"    First vertex: ({poly.vertices[0].x:.3f}, {poly.vertices[0].y:.3f})")
    
    # Test coordinate conversion directly
    print(f"\nCoordinate conversion test:")
    print(f"  1 inch = {_inch_to_mm(1.0):.3f} mm")
    print(f"  0.1 inch = {_inch_to_mm(0.1):.3f} mm")
    print(f"  0.01 inch = {_inch_to_mm(0.01):.3f} mm")
    
    # Check if gerber files are being parsed correctly
    print(f"\nGerber parsing details:")
    for layer in geom.layers:
        for f in layer.files:
            if f.format == "gerber":
                try:
                    gerber_layer = gerber.read(str(f.path))
                    print(f"\n{f.original_name}:")
                    
                    # Check original units
                    try:
                        gerber_layer.to_inch()  # This normalizes to inches
                        print(f"  Normalized to inches")
                    except Exception as e:
                        print(f"  Failed to normalize to inches: {e}")
                        continue
                    
                    # Show some primitive coordinates
                    primitives = getattr(gerber_layer, 'primitives', [])
                    print(f"  Primitives: {len(primitives)}")
                    
                    for i, prim in enumerate(primitives[:3]):
                        if hasattr(prim, 'vertices') and prim.vertices:
                            x, y = prim.vertices[0]
                            print(f"    Prim {i} first vertex (inches): ({x:.6f}, {y:.6f})")
                            print(f"    Prim {i} first vertex (mm): ({_inch_to_mm(x):.3f}, {_inch_to_mm(y):.3f})")
                        elif hasattr(prim, 'position'):
                            x, y = prim.position
                            print(f"    Prim {i} position (inches): ({x:.6f}, {y:.6f})")
                            print(f"    Prim {i} position (mm): ({_inch_to_mm(x):.3f}, {_inch_to_mm(y):.3f})")
                    
                except Exception as e:
                    print(f"  Failed to parse {f.original_name}: {e}")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python debug_coordinates.py <gerber_zip_path>")
        sys.exit(1)
    
    zip_path = sys.argv[1]
    debug_coordinate_system(zip_path)
