"""
Local demo web app for the recoupment/offset detector.

Upload one or more EOB PDFs (and optionally a claims ledger CSV), see
each one flagged individually plus an aggregate summary of total hidden
recoupment/offset dollars across the batch — the view that shows scale,
not just a single caught case.

Run:
    python app.py
Then open http://127.0.0.1:5050
"""

import os
import tempfile

from flask import Flask, render_template_string, request

import detector

app = Flask(__name__)

PAGE = """
<!doctype html>
<html>
<head>
<title>RemitGuard Recoupment Detector</title>
<style>
  body { font-family: -apple-system, sans-serif; max-width: 800px; margin: 40px auto; color: #1a1a1a; }
  h1 { font-size: 22px; }
  .sub { color: #666; margin-bottom: 24px; }
  .dropzone { border: 2px dashed #ccc; padding: 28px; border-radius: 10px; text-align: center; }
  .field { margin-top: 14px; text-align: left; font-size: 13px; color: #555; }
  .summary { margin-top: 28px; padding: 20px; border-radius: 10px; background: #1a1a1a; color: white; }
  .summary .big { font-size: 28px; font-weight: 700; color: #ff8a80; }
  .summary .row { display: flex; justify-content: space-between; padding: 4px 0; font-size: 14px; }
  .result { margin-top: 16px; padding: 16px; border-radius: 10px; }
  .flagged { background: #fdecec; border: 1px solid #f5b5b5; }
  .clean { background: #eaf7ec; border: 1px solid #a8dab5; }
  .warn { background: #fff8e1; border: 1px solid #ffe082; }
  .metric { display: flex; justify-content: space-between; padding: 3px 0; font-size: 14px; }
  .metric b { font-weight: 600; }
  .net-bad { color: #c0392b; font-weight: 700; }
  .net-ok { color: #27ae60; font-weight: 700; }
  .flag-line { font-family: monospace; background: #fff; padding: 6px; border-radius: 6px; margin-top: 4px; font-size: 12px; }
  .fname { font-weight: 600; margin-bottom: 6px; }
  button { background: #1a1a1a; color: white; border: none; padding: 10px 18px; border-radius: 6px; cursor: pointer; }
</style>
</head>
<body>
  <h1>Recoupment / Offset Detector</h1>
  <div class="sub">Upload one or more EOB PDFs. We flag hidden clawbacks and underpayments before they get booked as revenue.</div>

  <form method="post" enctype="multipart/form-data">
    <div class="dropzone">
      <input type="file" name="eobs" accept="application/pdf" multiple required>
      <div class="field">
        Optional claims ledger CSV (columns: claim_number, expected_amount) — catches mismatches even when the payer's wording isn't recognized yet:
        <br><input type="file" name="ledger" accept=".csv">
      </div>
      <br><button type="submit">Analyze EOB(s)</button>
    </div>
  </form>

  {% if summary %}
  <div class="summary">
    <div class="row"><span>EOBs processed</span><b>{{ summary.count }}</b></div>
    <div class="row"><span>Total paid (as shown on EOBs)</span><b>${{ '%.2f'|format(summary.total_paid) }}</b></div>
    <div class="row"><span>Total hidden recoupment/offset detected</span><span class="big">${{ '%.2f'|format(summary.total_flagged) }}</span></div>
    <div class="row"><span>Files with at least one flag</span><b>{{ summary.flagged_count }} / {{ summary.count }}</b></div>
  </div>
  {% endif %}

  {% for result in results %}
  <div class="result {{ 'flagged' if result.flags else ('warn' if result.extraction_warning else 'clean') }}">
    <div class="fname">{{ result.source_file.split('/')[-1] }}</div>
    {% if result.extraction_warning %}<div class="metric">⚠️ {{ result.extraction_warning }}</div>{% endif %}
    {% if result.claim_numbers %}<div class="metric"><span>Claim #</span><b>{{ result.claim_numbers|join(', ') }}</b></div>{% endif %}
    {% if result.dates_of_service %}<div class="metric"><span>Date of service</span><b>{{ result.dates_of_service|join(', ') }}</b></div>{% endif %}
    <div class="metric"><span>Billed</span><b>${{ '%.2f'|format(result.billed_amount) if result.billed_amount is not none else 'N/A' }}</b></div>
    <div class="metric"><span>Paid (line item on EOB)</span><b>${{ '%.2f'|format(result.paid_amount) if result.paid_amount is not none else 'N/A' }}</b></div>

    {% if result.flags %}
      <div class="metric"><span>⚠️ Recoupment / offset detected</span></div>
      {% for f in result.flags %}
        <div class="flag-line">[{{ f.payer_tag }}] {{ f.line }}</div>
      {% endfor %}
      <div class="metric"><span>Net cash actually received</span>
        <span class="net-bad">${{ '%.2f'|format(result.net_received) }}</span>
      </div>
    {% elif not result.extraction_warning %}
      <div class="metric"><span>No recoupment/offset detected</span></div>
    {% endif %}
  </div>
  {% endfor %}
</body>
</html>
"""


@app.route("/", methods=["GET", "POST"])
def index():
    results = []
    summary = None
    if request.method == "POST":
        files = request.files.getlist("eobs")
        ledger = None
        ledger_file = request.files.get("ledger")
        if ledger_file and ledger_file.filename:
            with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
                ledger_file.save(tmp.name)
                ledger = detector.load_ledger(tmp.name)
            os.unlink(tmp.name)

        for file in files:
            if not file.filename:
                continue
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                file.save(tmp.name)
                tmp_path = tmp.name
            try:
                results.append(detector.analyze(tmp_path, ledger=ledger))
            finally:
                os.unlink(tmp_path)

        if results:
            total_paid = sum(r.paid_amount for r in results if r.paid_amount)
            total_flagged = sum(amt for r in results for f in r.flags for amt in f.amounts_found)
            summary = {
                "count": len(results),
                "total_paid": total_paid,
                "total_flagged": total_flagged,
                "flagged_count": len([r for r in results if r.flags]),
            }

    return render_template_string(PAGE, results=results, summary=summary)


if __name__ == "__main__":
    app.run(port=5050, debug=True)
