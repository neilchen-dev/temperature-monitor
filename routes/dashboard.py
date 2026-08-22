from __future__ import annotations

import hmac
import json
from datetime import datetime, timedelta
from html import escape
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Blueprint, Response, request

import config
from services import db


dashboard_bp = Blueprint("dashboard", __name__)

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>温湿度监控 · 本地分析看板</title>
<script src="/static/chart.umd.min.js"></script>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px;
    font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
    background: #f4f6f9; color: #24292f;
  }
  h1 { font-size: 22px; margin: 0 0 4px; }
  .sub { color: #57606a; font-size: 13px; margin-bottom: 20px; }
  .grid { display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(480px, 1fr)); }
  .card {
    background: #fff; border: 1px solid #d0d7de; border-radius: 8px;
    padding: 16px; box-shadow: 0 1px 2px rgba(0,0,0,.05);
  }
  .card h2 { font-size: 15px; margin: 0 0 12px; color: #1f2328; }
  .chart-box { position: relative; height: 280px; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #eaeef2; }
  th { background: #f6f8fa; color: #57606a; font-weight: 600; white-space: nowrap; }
  td.num { font-variant-numeric: tabular-nums; }
  .ok { color: #1a7f37; } .warn { color: #9a6700; } .bad { color: #cf222e; }
  .empty { color: #8c959f; padding: 24px; text-align: center; }
  footer { margin-top: 20px; color: #8c959f; font-size: 12px; }
</style>
</head>
<body>
<h1>温湿度监控 · 本地分析看板</h1>
<div class="sub">数据来源：SQLite 本地镜像（飞书多维表格仍为事实源） · 统计窗口：最近 __DAYS__ 天 · 时区：__TIMEZONE__</div>
<div class="grid">
  <div class="card">
    <h2>超限与离线趋势（按天）</h2>
    <div class="chart-box"><canvas id="trendChart"></canvas></div>
  </div>
  <div class="card">
    <h2>各设备平均温度 / 湿度</h2>
    <div class="chart-box"><canvas id="avgChart"></canvas></div>
  </div>
  <div class="card" style="grid-column: 1 / -1;">
    <h2>设备总览</h2>
    __DEVICE_TABLE__
  </div>
</div>
<footer>离线时长为估算值（离线样本数 × 采样间隔），漏采或停机会导致偏差。接口：<code>/history/query</code> · <code>/history/stats/daily</code> · <code>/history/stats/devices</code></footer>
<script>
function showChartFallback() {
  document.querySelectorAll('.chart-box').forEach(el => {
    el.innerHTML = '<div class="empty">图表脚本加载失败，数据统计请使用 API 接口；下方设备总览表不受影响</div>';
  });
}
const data = __DATA__;
if (typeof Chart === 'undefined') {
  showChartFallback();
} else if (data.trend.dates.length > 0) {
  try {
    new Chart(document.getElementById('trendChart'), {
      type: 'bar',
      data: {
        labels: data.trend.dates,
        datasets: [
          { label: '温度超限次数', data: data.trend.tempAbnormal, backgroundColor: '#d29922' },
          { label: '湿度超限次数', data: data.trend.humidityAbnormal, backgroundColor: '#8250df' },
          { label: '离线次数', data: data.trend.offline, backgroundColor: '#cf222e' },
        ],
      },
      options: {
        maintainAspectRatio: false,
        scales: { y: { beginAtZero: true, ticks: { precision: 0 } } },
      },
    });
    new Chart(document.getElementById('avgChart'), {
      type: 'bar',
      data: {
        labels: data.avg.devices,
        datasets: [
          { label: '平均温度 (°C)', data: data.avg.temperature, backgroundColor: '#0969da', yAxisID: 'y' },
          { label: '平均湿度 (%RH)', data: data.avg.humidity, backgroundColor: '#2da44e', yAxisID: 'y1' },
        ],
      },
      options: {
        maintainAspectRatio: false,
        scales: {
          y: { position: 'left', title: { display: true, text: '°C' } },
          y1: { position: 'right', title: { display: true, text: '%RH' }, grid: { drawOnChartArea: false } },
        },
      },
    });
  } catch (err) {
    showChartFallback();
  }
} else {
  document.querySelectorAll('.chart-box').forEach(el => el.innerHTML = '<div class="empty">窗口内暂无快照数据</div>');
}
</script>
</body>
</html>
"""


def _auth_error():
    if not config.HISTORY_API_KEY:
        return "未配置 HISTORY_API_KEY，看板不可用", 503

    # The dashboard is rendered server-side, so browsers pass the key in the
    # query string (keep the URL private). Authorization: Bearer is also
    # accepted for programmatic access where headers are available.
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        provided_key = auth_header[len("Bearer "):].strip()
    else:
        provided_key = request.args.get("key", "")
    if not provided_key or not hmac.compare_digest(
        provided_key,
        config.HISTORY_API_KEY,
    ):
        return "鉴权失败：请通过 ?key=<HISTORY_API_KEY> 访问", 401
    return None


def _render_device_table(devices: list[dict[str, Any]]) -> str:
    if not devices:
        return '<div class="empty">暂无镜像数据</div>'

    rows = []
    for item in devices:
        status_class = "ok" if item["last_online_status"] == "在线" else "bad"
        # All string-ish DB values pass through escape() so a device name or
        # status containing HTML metacharacters can never inject markup.
        status_text = escape(str(item["last_online_status"] or "—"))
        temp = "—" if item["last_temperature"] is None else escape(f"{item['last_temperature']} °C")
        humidity = "—" if item["last_humidity"] is None else escape(f"{item['last_humidity']} %")
        offline_sec = item.get("estimated_offline_duration_sec") or 0
        rows.append(
            f"<tr><td>{escape(str(item['device']))}</td>"
            f"<td class='num'>{temp}</td><td class='num'>{humidity}</td>"
            f"<td class='{status_class}'>{status_text}</td>"
            f"<td class='num'>{item['snapshot_count']}</td>"
            f"<td class='num'>{item['report_count']}</td>"
            f"<td class='num'>{escape(str(item['last_sample_iso'] or '—'))}</td>"
            f"<td class='num'>{escape(str(item['last_report_at'] or '—'))}</td>"
            f"<td class='num'>{offline_sec // 60} 分钟</td></tr>"
        )
    return (
        "<table><tr><th>设备</th><th>最后温度</th><th>最后湿度</th><th>最后状态</th>"
        "<th>快照数</th><th>上报次数</th><th>最后快照时间</th><th>最后上报时间</th>"
        "<th>估算离线时长</th></tr>" + "".join(rows) + "</table>"
    )


@dashboard_bp.get("/dashboard")
def dashboard():
    error = _auth_error()
    if error:
        message, status_code = error
        return Response(message, status=status_code, mimetype="text/plain")
    if not db.is_enabled():
        return Response(
            "SQLite 本地镜像未启用或初始化失败",
            status=503,
            mimetype="text/plain",
        )

    try:
        timezone = ZoneInfo(config.HISTORY_TIMEZONE)
    except ZoneInfoNotFoundError:
        timezone = ZoneInfo("UTC")

    days = max(1, min(request.args.get("days", default=7, type=int) or 7, 90))
    end = datetime.now(timezone)
    start = end - timedelta(days=days)

    daily = db.fetch_daily_stats(
        start_ms=int(start.timestamp() * 1000),
        end_ms=int(end.timestamp() * 1000),
    )
    devices = db.fetch_device_stats()
    interval_seconds = config.HISTORY_INTERVAL_MINUTES * 60
    for item in devices:
        item["estimated_offline_duration_sec"] = (
            (item.get("offline_sample_count") or 0) * interval_seconds
        )

    trend_by_date: dict[str, dict[str, int]] = {}
    temperature_by_device: dict[str, list[float]] = {}
    humidity_by_device: dict[str, list[float]] = {}
    for row in daily:
        date_key = str(row["local_date"])
        bucket = trend_by_date.setdefault(
            date_key,
            {"temp": 0, "humidity": 0, "offline": 0},
        )
        bucket["temp"] += row["temp_abnormal_count"] or 0
        bucket["humidity"] += row["humidity_abnormal_count"] or 0
        bucket["offline"] += row["offline_count"] or 0
        if row["avg_temperature"] is not None:
            temperature_by_device.setdefault(
                str(row["device"]), []
            ).append(row["avg_temperature"])
        if row["avg_humidity"] is not None:
            humidity_by_device.setdefault(
                str(row["device"]), []
            ).append(row["avg_humidity"])

    # Unified device axis: every dataset is aligned to the same sorted device
    # list with None for missing values, so a device that has temperature but
    # no humidity (or vice versa) can never shift the other series' labels.
    devices_axis = sorted(set(temperature_by_device) | set(humidity_by_device))

    def _window_average(values_by_device: dict[str, list[float]], device: str):
        values = values_by_device.get(device)
        if not values:
            return None
        return round(sum(values) / len(values), 2)

    payload = {
        "trend": {
            "dates": sorted(trend_by_date),
            "tempAbnormal": [
                trend_by_date[date]["temp"] for date in sorted(trend_by_date)
            ],
            "humidityAbnormal": [
                trend_by_date[date]["humidity"] for date in sorted(trend_by_date)
            ],
            "offline": [
                trend_by_date[date]["offline"] for date in sorted(trend_by_date)
            ],
        },
        "avg": {
            "devices": devices_axis,
            "temperature": [
                _window_average(temperature_by_device, device)
                for device in devices_axis
            ],
            "humidity": [
                _window_average(humidity_by_device, device)
                for device in devices_axis
            ],
        },
    }

    # ``<`` escaping keeps embedded JSON from breaking out of the script tag.
    embedded_json = (
        json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    )
    html = (
        _PAGE_TEMPLATE
        .replace("__DAYS__", str(days))
        .replace("__TIMEZONE__", str(timezone))
        .replace("__DEVICE_TABLE__", _render_device_table(devices))
        .replace("__DATA__", embedded_json)
    )
    return Response(html, mimetype="text/html")
