@echo off
cd /d "%~dp0"
(
echo {
echo     "port": 5000,
echo     "last_project": "",
echo     "recent_projects": [],
echo     "urls": {
echo         "LOCAL": "http://localhost:8080"
echo     },
echo     "project_overrides": {}
echo }
) > config.json
echo Config reset.
pause
