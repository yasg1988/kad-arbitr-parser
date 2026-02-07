#!/bin/bash
Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp &
export DISPLAY=:99
sleep 1
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
