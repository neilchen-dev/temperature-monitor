# Home Assistant freshness contract

Home Assistant uses two independent paths for a device:

1. The existing state-change automation calls `POST /temperature`. This is a
   real measurement and advances `last_measurement_at` and the normal sample
   history.
2. A one-minute heartbeat automation calls `POST /temperature/heartbeat`.
   This confirms liveness and may carry the last/current values for runtime
   evaluation, but it does not append a measurement-history row or advance
   `last_measurement_at`.

The heartbeat automation must send `availability=unavailable` when any
required HA entity is `unavailable` or `unknown`. That signal is immediate;
the monitor does not wait for the freshness window to expire. A later
`availability=online` heartbeat or a valid measurement restores availability.

The endpoint uses the server receipt time for freshness. A source timestamp is
not trusted to make an old heartbeat look new. The status API exposes both
timestamps and the derived fields:

- `last_measurement_at` / `last_measurement_age_seconds`
- `last_heartbeat_at` / `last_heartbeat_age_seconds`
- `availability`
- `measurement_fresh`, `heartbeat_fresh`, `effective_fresh`

The effective rule is:

```text
effective_fresh = availability == online
                  AND (measurement_fresh OR heartbeat_fresh)
```

Heartbeat observations are tagged `record_type=HEARTBEAT` in the runtime
projection. They reuse the last known value for the alarm and PREWARNING
engines, so a stable value can continue a pending/alarm timer without being
counted as a new measurement. Formal ALARM remains higher priority than
PREWARNING.

The checked-in examples are intentionally for TH-01 only:

- `homeassistant/automation_th01.example.yaml` — value-change measurement
- `homeassistant/automation_th01_heartbeat.example.yaml` — one-minute and
  unavailable heartbeat
- `homeassistant/rest_command.yaml` — `python_heartbeat` command

Copy the heartbeat automation once for each production device and replace its
device ID and entity IDs. Do not replace the existing measurement automation
with the heartbeat command; both paths are required.
