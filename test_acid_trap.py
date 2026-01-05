#!/usr/bin/env python3
"""
Test script to run acid trap angle check and see debug output.
"""

import sys
from pathlib import Path

# Add the pcb_dfm package to the path
sys.path.insert(0, str(Path(__file__).parent))

try:
    from pcb_dfm.geometry.gerber_parser import build_board_geometry
    from pcb_dfm.ingest import ingest_gerber_zip
    from pcb_dfm.checks.impl_acid_trap_angle import run_acid_trap_angle
    from pcb_dfm.engine.context import CheckContext
    from pcb_dfm.engine.check_defs import CheckDefinition
except ImportError as e:
    print(f"Import error: {e}")
    print("Make sure the pcb_dfm package is available")
    sys.exit(1)

def test_acid_trap(zip_path):
    """Test acid trap angle check."""
    print(f"Testing acid trap angle check for: {zip_path}")
    
    # Ingest the gerber files
    try:
        ingest_result = ingest_gerber_zip(Path(zip_path))
        print(f"Ingested {len(ingest_result.files)} files")
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
    
    # Create a mock check definition
    check_def = CheckDefinition.from_dict({
        "id": "acid_trap_angle",
        "name": "Acid Trap Angle",
        "category_id": "copper_geometry",
        "severity": "error",
        "metric": {
            "kind": "angle",
            "units": "deg",
            "target": {"min": 90.0},
            "limits": {"min": 60.0},
        },
        "applies_to": {},
        "limits": {"min": 60.0},
        "description": "Detect sharp copper corners",
        "raw": {
            "min_area_mm2": 0.002,
            "max_area_mm2": 2000.0,
            "min_edge_length_mm": 0.02,
            "consider_planes": True,
        }
    })
    
    # Create context
    ctx = CheckContext(
        check_def=check_def,
        geometry=geom,
        ingest=ingest_result,
        ruleset_id="test",
        design_id="test_design",
        gerber_zip=Path(zip_path)
    )
    
    # Run the check
    try:
        result = run_acid_trap_angle(ctx)
        print(f"\nCheck result:")
        print(f"  Status: {result.status}")
        print(f"  Score: {result.score}")
        print(f"  Violations: {len(result.violations)}")
        
        if result.violations:
            for i, viol in enumerate(result.violations):
                print(f"  Violation {i}: {viol.message}")
                if viol.location:
                    print(f"    Location: {viol.location.layer} @ ({viol.location.x_mm}, {viol.location.y_mm})")
        
        if result.metric:
            print(f"  Metric: {result.metric.measured_value} {result.metric.units}")
            
    except Exception as e:
        print(f"Check execution failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python test_acid_trap.py <gerber_zip_path>")
        sys.exit(1)
    
    zip_path = sys.argv[1]
    test_acid_trap(zip_path)
