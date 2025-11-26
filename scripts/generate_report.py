from pathlib import Path
from pcb_dfm.io import load_dfm_result
from pcb_dfm.report import generate_markdown_report

result = load_dfm_result(Path("output/dfm_result.json"))
md = generate_markdown_report(result)
Path("output/dfm_report.md").write_text(md, encoding="utf-8")
