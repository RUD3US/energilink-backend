ENERGILINK APRIL 2026 TSV INJECTION

Recommended GitHub placement:

backend/inject_april_2026_from_tsv.py
backend/april_2026_readings_raw.tsv

Do not paste the raw TSV rows directly into an existing Python file.
Keep the TSV as a separate .tsv file, then let the Python script read it.

Render Shell commands:

1. Go to Render Dashboard > Backend Service > Shell

2. Make a backup:
cp /var/data/app.db /var/data/app-backup-before-april-2026-tsv-inject.db

3. First test only:
DRY_RUN=1 python inject_april_2026_from_tsv.py

4. If the dry run shows the correct totals, run live:
python inject_april_2026_from_tsv.py

Expected output:
Apr 1-14 total: 70.375
Apr 15-24 total: 60.170
April total: 130.545
Zero V/A/W rows: 0
Injected PF range: 0.8734 to 0.9650

After injection, refresh the website and choose April 2026.
