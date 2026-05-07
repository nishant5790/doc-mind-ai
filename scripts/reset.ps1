# scripts/reset.ps1 — hard reset of the local docker stack.
#
# Tears down all containers AND removes named volumes (`state`,
# `redis-data`) so the next `docker compose up` starts with truly
# empty disks. Then rebuilds and starts the stack.
#
# Note: with FRESH_START=true (default in docker-compose.yaml), a
# plain `docker compose restart` or `docker compose up` will already
# wipe app-level state on API boot. Use this script when you also
# want to drop the underlying volumes.

$ErrorActionPreference = "Stop"

$composeFile = Join-Path $PSScriptRoot "..\docker-compose.yaml"

Write-Host "==> docker compose down -v" -ForegroundColor Cyan
docker compose -f $composeFile down -v --remove-orphans

Write-Host "==> docker compose up -d --build" -ForegroundColor Cyan
docker compose -f $composeFile up -d --build

Write-Host "==> docker compose ps" -ForegroundColor Cyan
docker compose -f $composeFile ps
