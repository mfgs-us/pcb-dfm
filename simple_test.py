#!/usr/bin/env python3
"""
Simple test for run_dfm_bundle function
"""

from pathlib import Path

# Test the function directly
try:
    from pcb_dfm.engine.run import run_dfm_bundle
    
    gerber_zip = Path("Gerbers.zip")
    
    if not gerber_zip.exists():
        print(f"ERROR: {gerber_zip} not found")
        exit(1)
    
    print("Testing run_dfm_bundle()...")
    result = run_dfm_bundle(gerber_zip, ruleset_id="default", design_id="test_board")
    
    print("Result keys:", list(result.keys()))
    print("Overall score:", result.get('overall_score'))
    print("Stats:", result.get('stats'))
    print("Error:", result.get('error'))
    print("Check results count:", len(result.get('check_results', [])))
    
    # Verify contract
    required_keys = ['overall_score', 'check_results', 'stats', 'error']
    missing_keys = [k for k in required_keys if k not in result]
    
    if missing_keys:
        print(f"❌ Missing keys: {missing_keys}")
    else:
        print("✅ All required keys present")
    
    if result.get('error') is None:
        if result.get('overall_score', 0) > 0:
            print("✅ Non-zero score for successful run")
        else:
            print("❌ Zero score for successful run")
            
        stats = result.get('stats', {})
        if stats.get('total', 0) > 0:
            print("✅ Non-zero total checks")
        else:
            print("❌ Zero total checks")
    else:
        print(f"❌ Function returned error: {result.get('error')}")
        
except Exception as e:
    print(f"❌ Exception occurred: {e}")
    import traceback
    traceback.print_exc()
