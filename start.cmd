@echo off
setlocal

cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
if not exist data mkdir data

where uv >nul 2>&1 || (
    echo [!!] uv is not on PATH. Install it: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
    goto fail
)
echo [ok] uv
uv sync --inexact -q || (echo [!!] uv sync failed & goto fail)
echo [ok] python env

if not exist .env (
    copy .env.example .env >nul
    echo [!!] Created .env from .env.example. Fill in POLARIS_DSN and POSTGRES_PASSWORD, then run this again.
    goto fail
)
echo [ok] .env

where npm >nul 2>&1 || (echo [!!] Node.js/npm is not on PATH. Install Node.js from https://nodejs.org/ & goto fail)
if not exist web\node_modules (
    echo [..] installing web dependencies
    pushd web
    if exist package-lock.json (call npm ci) else (call npm install)
    if errorlevel 1 (popd & echo [!!] npm install failed & goto fail)
    popd
)
echo [ok] node

where wslc >nul 2>&1 || (echo [!!] wslc is not on PATH. Update WSL: wsl --update & goto fail)
echo [ok] wslc

wslc exec polaris-pg pg_isready -q -U postgres -d polaris >nul 2>&1 && goto pg_ready
echo [..] starting the database
wslc start polaris-pg >nul 2>&1 || (
    set "PGPW=polaris"
    for /f "usebackq tokens=1,* delims==" %%a in (".env") do if "%%a"=="POSTGRES_PASSWORD" set "PGPW=%%b"
    call :create_pg || (echo [!!] wslc could not create the database container & goto fail)
)
set /a tries=0
:wait_pg
wslc exec polaris-pg pg_isready -q -U postgres -d polaris >nul 2>&1 && goto pg_ready
set /a tries+=1
if %tries% geq 60 (echo [!!] The database did not become ready within 2 minutes. & goto fail)
ping -n 3 127.0.0.1 >nul
goto wait_pg
:pg_ready
echo [ok] database

.venv\Scripts\polaris-migrate.exe > data\migrate.log 2>&1 || (
    type data\migrate.log
    echo [!!] The database schema is not current; see above. If migrations are pending, back it up, then run: .venv\Scripts\python.exe -m polaris.cli.migrate --apply
    goto fail
)
echo [ok] schema

netstat -ano | findstr /r /c:":8000 .*LISTENING" >nul && (echo [ok] API already running) || (
    echo [..] starting API
    powershell -NoProfile -Command "Start-Process cmd -WindowStyle Hidden -ArgumentList '/c .venv\Scripts\python.exe -m uvicorn polaris.app:app --port 8000 >> data\api.log 2>&1'"
)
netstat -ano | findstr /r /c:":5173 .*LISTENING" >nul && (echo [ok] web already running) || (
    echo [..] starting web
    powershell -NoProfile -Command "Start-Process cmd -WindowStyle Hidden -WorkingDirectory web -ArgumentList '/c npm run dev >> ..\data\web.log 2>&1'"
)

set /a tries=0
:wait_api
curl -s -o nul -m 2 http://localhost:8000/api/collections && goto api_ready
set /a tries+=1
if %tries% geq 60 (
    powershell -NoProfile -Command "Get-Content data\api.log -Tail 30"
    echo [!!] The API did not answer within 2 minutes; see data\api.log.
    goto fail
)
ping -n 3 127.0.0.1 >nul
goto wait_api
:api_ready
echo [ok] API on :8000

set /a tries=0
:wait_web
curl -s -o nul -m 2 http://localhost:5173/ && goto web_ready
set /a tries+=1
if %tries% geq 30 (echo [!!] The web page did not answer within 1 minute; see data\web.log. & goto fail)
ping -n 3 127.0.0.1 >nul
goto wait_web
:web_ready
echo [ok] web on :5173
start "" http://localhost:5173/
exit /b 0

:create_pg
wslc run -d --name polaris-pg --shm-size 1g --stop-signal SIGINT --stop-timeout 60 -p 5432:5432 ^
    -v polaris_pgdata:/var/lib/postgresql/data ^
    -e "POSTGRES_PASSWORD=%PGPW%" -e POSTGRES_DB=polaris ^
    --health-cmd "pg_isready -U postgres -d polaris" --health-interval 5s --health-timeout 5s --health-retries 10 ^
    pgvector/pgvector@sha256:cf134a767f474095eeba57e0117be8e568e011a63f33fbf252f14c9b760f8e6f ^
    postgres -c shared_buffers=2GB -c effective_cache_size=5GB -c work_mem=32MB -c maintenance_work_mem=1GB ^
    -c max_wal_size=8GB -c min_wal_size=1GB -c checkpoint_timeout=15min -c checkpoint_completion_target=0.9 ^
    -c wal_compression=lz4 -c default_toast_compression=lz4 -c random_page_cost=4 -c jit=off ^
    -c track_io_timing=on -c log_min_duration_statement=500ms >nul
exit /b %errorlevel%

:fail
pause
exit /b 1
