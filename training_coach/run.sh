#!/bin/sh
set -e
cd /opt/training_coach
exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8099 --proxy-headers --no-access-log
