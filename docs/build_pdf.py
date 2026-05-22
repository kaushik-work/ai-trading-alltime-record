"""Convert docs/STRATEGY.md to a clean PDF."""
from pathlib import Path
from markdown_pdf import MarkdownPdf, Section

ROOT = Path(__file__).parent
md_path  = ROOT / "STRATEGY.md"
pdf_path = ROOT / "STRATEGY.pdf"

text = md_path.read_text(encoding="utf-8")

pdf = MarkdownPdf(toc_level=2, optimize=True)
pdf.add_section(Section(text, toc=True))
pdf.meta["title"]    = "NIFTY Q5 Multi-Strategy Shadow"
pdf.meta["author"]   = "The Gaint Company"
pdf.meta["subject"]  = "Strategy decisions, formulas, risk controls, weekly plan"
pdf.save(str(pdf_path))
print(f"Wrote {pdf_path}  ({pdf_path.stat().st_size // 1024} KB)")
