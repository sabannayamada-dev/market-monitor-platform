@echo off
cd /d "%~dp0"
python company_collection.py --registry work\data_source_registry_template.json --source-id job_file
pause
