@echo off
REM Narada chat bridge — Windows autostart wrapper.
REM
REM Mirrors the pattern Hermes uses for its gateway service: a .cmd
REM script that sets up the environment and launches the bridge silently
REM via pythonw (no console window). A copy of this file lives in the
REM Startup folder so it runs at user logon.
REM
REM Source of truth: C:\Projects\prana\scripts\narada-bridge.cmd
REM Startup copy:    %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Narada_Chat_Bridge.cmd

REM Defensive: strip any inherited Anthropic API key so claude -p falls
REM back to the Max subscription. This is the same posture the heartbeat
REM .bat takes — see scripts/heartbeat.bat.example for the precedent
REM and prior incident.
set ANTHROPIC_API_KEY=
set ANTHROPIC_AUTH_TOKEN=

REM Logs land here. .narada is the canonical user-data root.
set LOG_DIR=%USERPROFILE%\.narada\logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM pythonw runs the script without spawning a console window. stdout
REM and stderr go to the log file. start /B detaches so the .cmd exits
REM immediately and Windows considers logon complete.
start /B "" pythonw "C:\Projects\prana\scripts\narada_chat_bridge.py" >> "%LOG_DIR%\narada-bridge.log" 2>&1

exit /b 0
