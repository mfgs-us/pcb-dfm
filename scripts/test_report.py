# scripts/test_report.py
from pathlib import Path

from pcb_dfm.io import load_dfm_result
from pcb_dfm.report import generate_markdown_report

result_path = Path("testdata/example_result.json")
result = load_dfm_result(result_path)

md = generate_markdown_report(result)
print(md)
