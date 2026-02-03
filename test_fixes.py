#!/usr/bin/env python3
"""
Test the fixes for the 3 implementation bugs
"""

from pathlib import Path

try:
    from pcb_dfm.engine.run import run_dfm_bundle
    
    gerber_zip = Path("Gerbers.zip")
    
    if not gerber_zip.exists():
        print(f"ERROR: {gerber_zip} not found")
        exit(1)
    
    print("Testing run_dfm_bundle() after fixes...")
    result = run_dfm_bundle(gerber_zip, ruleset_id="default", design_id="test_board")
    
    print(f"Overall score: {result.get('overall_score')}")
    print(f"Stats: {result.get('stats')}")
    print(f"Error: {result.get('error')}")
    print(f"Check results count: {len(result.get('check_results', []))}")
    
    # Check if we fixed the issues
    if result.get('error') is None:
        print("✅ No fatal errors")
        if result.get('overall_score', 0) > 0:
            print("✅ Non-zero score")
        else:
            print("❌ Still zero score")
    else:
        print("❌ Still has errors")
        
except Exception as e:
    print(f"❌ Exception: {e}")
    import traceback
    traceback.print_exc()
