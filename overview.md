# UI Refinement — Industrial HMI pass

**File:** `G:\temperature-monitor\static\console.html`
**Date:** 2026-08-31
**Scope:** Visual only — no backend, API, or DB contract changes.

## Goal
Mature the prototype-looking monitoring console into a restrained, operator-grade
HMI. Keep the same five-tab structure, same data, same endpoints. Improve
hierarchy, density, borders, typography, and empty-state behaviour.

## What changed

### CSS
- Replaced the two-layer stylesheet (old SaaS layer + "Industrial HMI refactor"
  override) with a **single, clean stylesheet**. No more inherited gradients,
  shadows, KPI cards, or boxed tabs.
- Tokenised neutrals (`--bg`, `--surface`, `--ink`, `--muted`, `--line`,
  `--line-soft`, `--control-line`) and a single teal accent (`--brand`).
- 2px corner radius everywhere — no pill shapes.
- `box-shadow: none` and no gradients on any element.

### Header (56px, single row)
- Left: page title (18px/600) + small subtitle.
- Right: connection status · refresh mode · last refresh time · 凭据设置.
- No hero banner, no gradient, no decorative pattern.

### Tabs (separate row, underline indicator)
- Full-bleed row under the header.
- Active tab uses a 2px teal underline; no boxed/pill tabs.
- Inactive tabs: muted text; hover lifts to ink + soft surface.

### Left summary (228px, intentional)
- Five compact label/value rows: **设备 / 在线 / 离线 / 活动告警 / 最近更新**,
  each with a secondary line of context.
- Collector status row at the bottom (dot + 文本).
- No KPI cards, no left accent bars, no rounded metric blocks.

### Main realtime area
- Device table is the dominant content.
- Table always renders its header. When data is missing, a single muted
  placeholder row replaces the body — no huge white panel, no centred
  call-to-action, no decorative illustration.
- "配置凭据" button lives in the toolbar (not centred in the empty state).

### Device table
- Columns: 设备 / 区域 / 温度 / 湿度 / 控制类型 / 操作 / 状态 / 告警 / 更新时间.
- Compact rows (6px vertical padding) with subtle horizontal separators.
- 趋势 / 阈值 row actions in teal, muted when secondary.
- Selected row uses a soft brand-soft background.

### Toolbar
- Single compact row above the table.
- Equal-height 28px controls, 2px border-radius, bottom border only.
- Order: 搜索 / 状态筛选 / 来源筛选 / spacer / 刷新 / 配置凭据 (when no key) / 自动刷新.

### Border reduction
- Outer chrome uses pane geometry + spacing, not borders.
- Borders kept only for: summary panel, table container, important controls.
- Tabs use underline; toolbar uses bottom border only.

### Typography
- Page title: 18px / 600.
- Section / card title: 14px / 600.
- Body / table: 12px / 400.
- Metadata: 11px / muted.
- Tabular numerals for all readings and KPIs.

### Status bar (footer)
- Thin (26px) bottom row pinned to the viewport bottom via flex column.
- Format: `SQLite OK | N devices | N online | N alarms | refresh 10s | updated HH:MM:SS`.
- No heavy border; uses a soft surface background + 1px top line.

## JS
- `renderCards` now **always builds the table**, even with no data — placeholder
  row carries the operational message.
- `renderDeviceTable` and `renderThresholds` got the same treatment so every
  tab keeps its table skeleton.
- New helper `placeholderRow(colspan, main, sub)` for consistent empty rows.
- Toolbar configure button (`#toolbarConfigureBtn`) is shown only when no key
  is in sessionStorage; it opens the same credentials dialog.
- Header refresh-mode indicator (`#autoRefreshState`) updates between
  "自动刷新 · 10s" / "手动刷新" via `setAutoRefresh()`.
- Removed dead `deviceCard` and `cardState` functions (the table renderer
  superseded them).
- Removed `kpiCollector` and `emptyCredentialsBtn` references.
- No-credentials refresh path now also calls `renderDeviceTable` and
  `renderThresholds` so the table skeleton appears across all tabs.
- Chart cosmetics realigned to the new neutral palette (legend, ticks, grid,
  tooltip background, 2px corner radius).

## Verification
- Inline JS passes `node --check`.
- All 13 tests in `tests/test_thresholds_api.py` pass, including
  `test_console_serves_spa_shell_without_key`.
- Visual verification (Chromium via `agent-browser`) on the local Flask
  server (HISTORY_API_KEY=devkey):
  - Monitor tab, empty state: clean header, summary panel with 5 rows,
    table skeleton with "无监控数据 / 需要配置访问凭据" placeholder,
    "配置凭据" in toolbar, thin status bar pinned at bottom.
  - Monitor tab, connected: green "已连接" dot, 1 device row (PLC-01,
    MODBUS, 离线, em-dash readings, 08-25 15:42:41), footer shows
    `SQLite OK | 1 个设备 | 0 在线 | 0 告警 | 刷新 10 秒 | 更新 HH:MM:SS`.
  - 历史趋势 / 设备与事件 / 阈值设置 tabs render consistently with the
    same restrained aesthetic.

## Out of scope
- No backend, API, or data contract changes.
- No new endpoints, no new auth model, no new persistence.
- No redesign of the 5-tab structure or data semantics.
