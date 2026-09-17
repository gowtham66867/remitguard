"""Generates a synthetic EOB whose offset is written in wording the pattern
library has never seen — no 'recoup', no 'offset', no 'neg bal'.

This is the recall gap the Moss semantic layer closes. Regex-only detection
returns zero flags on this file and reports the full $9,480.00 as received;
with Moss enabled the reworded line is flagged and net_received drops to the
amount the practice will actually bank.

    python make_sample3.py
    cd platform && python run_pipeline.py ../samples/regional_reworded_sample.pdf
"""

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

OUT_PATH = "samples/regional_reworded_sample.pdf"

lines = [
    "Meridian Regional Health Plan",
    "Provider Remittance Advice",
    "",
    "Provider: Behavioral Health Associates",
    "Claim # MRH20260412",
    "DOS 2/03/2026",
    "",
    "Billed Amount: $14,600.00",
    "Total Payment: $9,480.00",
    "",
    "Remittance detail:",
    "  Settlement of an outstanding accounts receivable      3,240.00",
    "  balance against this payment cycle.",
    "",
    "  Contractual adjustment per provider agreement         5,120.00",
    "  Patient responsibility after plan payment               240.00",
    "",
    "Questions: provider.services@meridianregional.example",
]

c = canvas.Canvas(OUT_PATH, pagesize=letter)
text = c.beginText(72, 720)
text.setFont("Helvetica", 11)
for line in lines:
    text.textLine(line)
c.drawText(text)
c.save()
print(f"wrote {OUT_PATH}")
