@echo off
rem TTS one-click launcher: starts the tray app (service is spawned by run.py).
rem Safe to double-click repeatedly: a mutex inside run.py makes the second
rem instance exit silently, so this never spawns duplicates.
start "" /D "C:\tts" "C:\tts\.venv\Scripts\pythonw.exe" "C:\tts\run.py" --tray
