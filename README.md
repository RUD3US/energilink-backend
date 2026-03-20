# EnergiLink Backend

FastAPI backend for the EnergiLink power monitoring system.

## Main features
- auth
- realtime/archive ingest
- notes on power graphs
- GEMP dynamic kWh
- GEMP DOCX export
- weekly/monthly report scheduler

## Local run
```bash
uvicorn main:app --host 0.0.0.0 --port 8080
