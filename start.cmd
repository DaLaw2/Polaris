@echo off
setlocal

cd /d "%~dp0"
set "DOCKER_EXE=C:\Program Files\Docker\Docker\Docker Desktop.exe"
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

docker info >nul 2>&1 && goto docker_ready
echo [..] starting Docker Desktop
start "" "%DOCKER_EXE%"
set /a tries=0
:wait_docker
ping -n 3 127.0.0.1 >nul
docker info >nul 2>&1 && goto docker_ready
set /a tries+=1
if %tries% lss 90 goto wait_docker
echo [!!] Docker did not come up within 3 minutes.
goto fail
:docker_ready
echo [ok] docker

set "pg="
for /f %%s in ('docker inspect -f "{{.State.Status}}" polaris-pg 2^>nul') do set "pg=%%s"
if not "%pg%"=="running" (
    echo [..] starting the database
    docker compose up -d pgvector >nul || (echo [!!] docker compose up failed & goto fail)
)
set /a tries=0
:wait_pg
set "health="
for /f %%h in ('docker inspect -f "{{.State.Health.Status}}" polaris-pg 2^>nul') do set "health=%%h"
if "%health%"=="healthy" goto pg_ready
set /a tries+=1
if %tries% geq 60 (echo [!!] The database did not become healthy within 2 minutes. & goto fail)
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

:fail
pause
exit /b 1
