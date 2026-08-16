[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "未检测到 Docker。请先安装并启动 Docker Desktop：https://www.docker.com/products/docker-desktop/"
}

if (-not (Test-Path -LiteralPath ".env")) {
    Copy-Item -LiteralPath ".env.example" -Destination ".env"
    Write-Host "已创建 .env。请填写飞书凭据和 DEVICE_RECORD_MAP 后，再次运行此脚本。" -ForegroundColor Yellow
    exit 1
}

docker compose up -d
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "部署完成。健康检查：http://localhost:5000/health" -ForegroundColor Green
