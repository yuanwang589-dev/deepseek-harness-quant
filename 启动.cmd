@echo off
chcp 65001 >nul
REM DSHQuant launcher: venv first, then bundled Python, then system Python.
REM PURE ASCII on purpose - cmd.exe parses .cmd with the OEM codepage (GBK
REM on Chinese Windows), so non-ASCII bytes here would corrupt lines.
cd /d "%~dp0"

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

REM Force the cache dir to data\cache: 136 places in the project hardcode
REM data/cache/*.db, while launcher.py defaults LWQUANT_CACHE_DIR to <repo>\data.
REM Setting it here keeps every reader on the same directory.
set "LWQUANT_CACHE_DIR=%CD%\data\cache"

if exist "..\.venv\Scripts\python.exe" (
  "..\.venv\Scripts\python.exe" launcher.py
  goto :end
)
if exist "runtime\python\python.exe" (
  "runtime\python\python.exe" launcher.py
  goto :end
)
if exist "portable-py\Scripts\python.exe" (
  "portable-py\Scripts\python.exe" launcher.py
  goto :end
)
python launcher.py
:end
pause
