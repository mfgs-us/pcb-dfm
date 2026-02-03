#!/usr/bin/env python3
"""
Test script for the new run_dfm_bundle() function
"""

from pathlib import Path
import sys
import os

# Add the pcb_dfm package to the path
sys.path.insert(0, str(Path(__file__).parent / "pcb_dfm"))

from pcb_dfm.engine.run import run_dfm_bundle

def test_run_dfm_bundle():
    """Test the run_dfm_bundle function with the sample Gerbers.zip"""
    
    gerber_zip = Path("Gerbers.zip")
    
    if not gerber_zip.exists():
        print(f"ERROR: {gerber_zip} not found")
        return
    
    print("Testing run_dfm_bundle()...")
    print("=" * 50)
    
    result = run_dfm_bundle(gerber_zip, ruleset_id="default", design_id="test_board")
    
    print("\nResult structure:")
    print(f"overall_score: {result.get('overall_score')}")
    print(f"check_results count: {len(result.get('check_results', []))}")
    print(f"stats: {result.get('stats')}")
    print(f"error: {result.get('error')}")
    
    # Verify the contract
    assert "overall_score" in result, "Missing overall_score"
    assert "check_results" in result, "Missing check_results"
    assert "stats" in result, "Missing stats"
    assert "error" in result, "Missing error"
    
    # Verify stats structure
    stats = result["stats"]
    assert "total" in stats, "Missing stats.total"
    assert "passed" in stats, "Missing stats.passed"
    assert "warnings" in stats, "Missing stats.warnings"
    assert "failed" in stats, "Missing stats.failed"
    
    # Key requirement: clean board should still have non-zero score
    if result["error"] is None:
        assert result["overall_score"] > 0, "overall_score should be > 0 for successful runs"
        assert stats["total"] > 0, "stats.total should be > 0 when checks run"
    
    print("\n✅ Contract validation passed!")
    
    # Show a few sample results
    if result["check_results"]:
        print(f"\nFirst {min(3, len(result['check_results']))} issue(s):")
        for i, check_result in enumerate(result["check_results"][:3]):
            print(f"  {i+1}. {check_result.get('check_id', 'unknown')}: {check_result.get('status', 'unknown')}")
    else:
        print("\n✅ No issues found - clean board!")
        print(f"   Score: {result['overall_score']:.1f}")
        print(f"   Checks run: {stats['total']}")

if __name__ == "__main__":
    test_run_dfm_bundle()
