"""
Unit test for run_dfm_bundle() function - clean board scenario
"""

import unittest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import sys
import os

# Add the pcb_dfm package to the path
sys.path.insert(0, str(Path(__file__).parent / "pcb_dfm"))

from pcb_dfm.engine.run import run_dfm_bundle


class TestRunDfmBundle(unittest.TestCase):
    """Test cases for run_dfm_bundle function"""
    
    @patch('pcb_dfm.engine.run.load_check_definitions_for_ruleset')
    @patch('pcb_dfm.engine.run.get_check_runner')
    @patch('pcb_dfm.engine.run.ingest_gerber_zip')
    @patch('pcb_dfm.engine.run.build_board_geometry')
    def test_clean_board_returns_nonzero_score(self, mock_build_geometry, mock_ingest, 
                                               mock_get_runner, mock_load_checks):
        """Test that a clean board still returns non-zero score and proper stats"""
        
        # Setup mocks
        mock_gerber_zip = Path("test_gerbers.zip")
        
        # Mock check definitions (3 checks that all pass)
        mock_check_def = Mock()
        mock_check_def.id = "test_check_1"
        mock_load_checks.return_value = [mock_check_def]
        
        # Mock ingest and geometry
        mock_ingest_result = Mock()
        mock_ingest.return_value = mock_ingest_result
        mock_geom = Mock()
        mock_build_geometry.return_value = mock_geom
        
        # Mock check runner that returns a passing result
        mock_check_result = Mock()
        mock_check_result.status = "pass"
        mock_check_result.violations = []
        mock_check_result.to_dict.return_value = {
            "check_id": "test_check_1",
            "status": "pass",
            "score": 100.0,
            "violations": []
        }
        
        mock_runner = Mock(return_value=mock_check_result)
        mock_get_runner.return_value = mock_runner
        
        # Run the function
        result = run_dfm_bundle(mock_gerber_zip, ruleset_id="default", design_id="test_board")
        
        # Verify the contract
        self.assertIn("overall_score", result)
        self.assertIn("check_results", result)
        self.assertIn("stats", result)
        self.assertIn("error", result)
        
        # Key assertions for clean board scenario
        self.assertIsNone(result["error"], "Should be no error for clean board")
        self.assertGreater(result["overall_score"], 0, "Overall score should be > 0 for clean board")
        self.assertEqual(result["overall_score"], 100.0, "Clean board should score 100.0")
        
        # Verify stats
        stats = result["stats"]
        self.assertGreater(stats["total"], 0, "Total checks should be > 0")
        self.assertEqual(stats["total"], 1, "Should have run 1 check")
        self.assertEqual(stats["passed"], 1, "Should have 1 passed check")
        self.assertEqual(stats["warnings"], 0, "Should have 0 warnings")
        self.assertEqual(stats["failed"], 0, "Should have 0 failed checks")
        
        # For clean board, check_results should be empty (issues-only)
        self.assertEqual(len(result["check_results"]), 0, "Clean board should have no issues")
    
    @patch('pcb_dfm.engine.run.load_check_definitions_for_ruleset')
    @patch('pcb_dfm.engine.run.get_check_runner')
    @patch('pcb_dfm.engine.run.ingest_gerber_zip')
    @patch('pcb_dfm.engine.run.build_board_geometry')
    def test_board_with_issues_returns_proper_score(self, mock_build_geometry, mock_ingest,
                                                    mock_get_runner, mock_load_checks):
        """Test that board with issues returns proper score and includes issues"""
        
        # Setup mocks
        mock_gerber_zip = Path("test_gerbers.zip")
        
        # Mock check definitions (2 checks: one pass, one fail)
        mock_check_def_1 = Mock()
        mock_check_def_1.id = "passing_check"
        mock_check_def_2 = Mock()
        mock_check_def_2.id = "failing_check"
        mock_load_checks.return_value = [mock_check_def_1, mock_check_def_2]
        
        # Mock ingest and geometry
        mock_ingest_result = Mock()
        mock_ingest.return_value = mock_ingest_result
        mock_geom = Mock()
        mock_build_geometry.return_value = mock_geom
        
        # Mock check results
        mock_pass_result = Mock()
        mock_pass_result.status = "pass"
        mock_pass_result.violations = []
        
        mock_fail_result = Mock()
        mock_fail_result.status = "fail"
        mock_fail_result.violations = [Mock()]  # Has violations
        mock_fail_result.to_dict.return_value = {
            "check_id": "failing_check",
            "status": "fail",
            "score": 0.0,
            "violations": [{"severity": "error", "message": "Test violation"}]
        }
        
        def mock_runner_factory(check_id):
            if check_id == "passing_check":
                return Mock(return_value=mock_pass_result)
            else:
                return Mock(return_value=mock_fail_result)
        
        mock_get_runner.side_effect = mock_runner_factory
        
        # Run the function
        result = run_dfm_bundle(mock_gerber_zip, ruleset_id="default", design_id="test_board")
        
        # Verify the contract
        self.assertIsNone(result["error"])
        self.assertGreater(result["overall_score"], 0)
        self.assertLess(result["overall_score"], 100)  # Should be penalized for failure
        
        # Verify stats
        stats = result["stats"]
        self.assertEqual(stats["total"], 2, "Should have run 2 checks")
        self.assertEqual(stats["passed"], 1, "Should have 1 passed check")
        self.assertEqual(stats["failed"], 1, "Should have 1 failed check")
        
        # Should include the failing check in check_results
        self.assertEqual(len(result["check_results"]), 1, "Should have 1 issue")
        self.assertEqual(result["check_results"][0]["check_id"], "failing_check")
    
    @patch('pcb_dfm.engine.run.load_check_definitions_for_ruleset')
    def test_error_handling_returns_proper_structure(self, mock_load_checks):
        """Test that errors are handled gracefully and return proper structure"""
        
        # Setup mock to raise exception
        mock_load_checks.side_effect = Exception("Test error")
        
        mock_gerber_zip = Path("test_gerbers.zip")
        
        # Run the function
        result = run_dfm_bundle(mock_gerber_zip, ruleset_id="default", design_id="test_board")
        
        # Verify error structure
        self.assertIn("overall_score", result)
        self.assertIn("check_results", result)
        self.assertIn("stats", result)
        self.assertIn("error", result)
        
        self.assertEqual(result["overall_score"], 0.0)
        self.assertEqual(len(result["check_results"]), 0)
        self.assertEqual(result["stats"], {"total": 0, "passed": 0, "warnings": 0, "failed": 0})
        self.assertIsNotNone(result["error"])
        self.assertIn("Test error", result["error"])


if __name__ == "__main__":
    unittest.main()
