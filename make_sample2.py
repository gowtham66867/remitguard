"""Generates a second synthetic EOB with NO recognizable recoupment phrase,
to prove the ledger-reconciliation catch-all works even when the payer's
wording isn't in our pattern library yet."""

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

OUT_PATH = "samples/cigna_unknown_phrasing_sample.pdf"

lines = [
    "Cigna Healthcare",
    "Explanation of Benefits",
    "",
    "Patient: John Doe",
    "Claim # CIGNA9988776",
    "DOS 1/15/2026",
    "",
    "Billed Amount: $4,000.00",
    "Total Payment: $1,200.00",
    "",
    "See attached schedule for details.",
]

c = canvas.Canvas(OUT_PATH, pagesize=letter)
text = c.beginText(72, 720)
text.setFont("Helvetica", 11)
for line in lines:
    text.textLine(line)
c.drawText(text)
c.save()
print(f"wrote {OUT_PATH}")
