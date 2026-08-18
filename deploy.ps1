[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "未检测到 Docker。请先安装并启动 Docker Desktop：https://www.docker.com/products/docker-desktop/"
}

if (-not (Test-Path -LiteralPath ".env")) {
    Copy-Item -LiteralPath ".env.example" -Destination ".env"
    Write-Host "已创建 .env。请填写飞书凭据；record_id 默认自动识别，随后再次运行此脚本。" -ForegroundColor Yellow
    exit 1
}

Write-Host "正在拉取 compose.yaml 配置的 Temperature Monitor 镜像..." -ForegroundColor Cyan
docker compose pull
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "正在更新容器并等待健康检查..." -ForegroundColor Cyan
docker compose up -d --remove-orphans --wait --wait-timeout 120
if ($LASTEXITCODE -ne 0) {
    docker compose logs --tail 100 temperature-monitor
    exit $LASTEXITCODE
}

$portBinding = docker compose port temperature-monitor 5000 | Select-Object -First 1
$publishedPort = if ($portBinding -match ":(?<port>\d+)$") { $Matches.port } else { "5000" }
Write-Host "部署完成，容器健康。健康检查：http://localhost:$publishedPort/health" -ForegroundColor Green
