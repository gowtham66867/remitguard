"""Generates a synthetic EOB PDF modeled on the real Anthem case from the
RemitGuard billing thread (Faten Haddad, claim 2021223CF200498) for demo/testing."""

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

OUT_PATH = "samples/anthem_haddad_sample.pdf"

lines = [
    "Anthem Blue Cross Blue Shield",
    "Explanation of Benefits",
    "",
    "Patient: Faten Haddad",
    "Claim # 2021223CF200498",
    "DOS 3/27/2021",
    "",
    "Billed Amount: $25,000.00",
    "Total Payment: $17,875.09",
    "",
    "OUTSTANDING NEGBAL WITH DIFFER: $18,020.11",
    "",
    "This payment has been applied against a prior outstanding balance",
    "on file for this provider. See remittance detail for prior claim history.",
]

c = canvas.Canvas(OUT_PATH, pagesize=letter)
text = c.beginText(72, 720)
text.setFont("Helvetica", 11)
for line in lines:
    text.textLine(line)
c.drawText(text)
c.save()
print(f"wrote {OUT_PATH}")
