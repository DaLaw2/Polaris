@echo off

powershell -NoProfile -Command "$ids = Get-NetTCPConnection -State Listen -LocalPort 8000,5173 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique; if (-not $ids) { '[ok] nothing listening on 8000/5173' }; foreach ($id in $ids) { $top = $id; $p = Get-CimInstance Win32_Process -Filter \"ProcessId=$id\"; while ($p = Get-CimInstance Win32_Process -Filter \"ProcessId=$($p.ParentProcessId)\") { if ($p.CommandLine -notmatch 'uvicorn|vite|npm') { break }; $top = $p.ProcessId }; taskkill /T /F /PID $top }"
echo [ok] stopped. The database is still running; to stop it: docker compose stop pgvector
