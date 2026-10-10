from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

import config
from integrations.feishu_records import FeishuRawRecord
from integrations.feishu_writers import FeishuInspectionRecordWriter
from routes.api import api_bp
from runtime import bootstrap


@pytest.mark.parametrize('area,parent,records,expected', [
    ('OUTSIDE', None, (), 403),
    ('A', 'rec-parent', (FeishuRawRecord('rec-parent', {'仓库区域': 'OUTSIDE'}),), 403),
    ('A', 'missing', (), 403),
    ('A', 'rec-parent', (FeishuRawRecord('rec-parent', {'仓库区域': 'A'}),), 201),
    ('A', None, (), 201),
])
def test_inspection_scope_binds_actual_region_and_parent(area, parent, records, expected):
    app = Flask(__name__)
    app.register_blueprint(api_bp)
    with patch.multiple(config, HISTORY_API_KEY='audit-key', AUTOMATION_MODE='active',
                        FEISHU_WRITE_ENABLED=True, ACTIVE_DEVICE_IDS=('TH-10',)), \
            patch('routes.api.FeishuBitableRecordSource') as source, \
            patch('routes.api.FeishuBitableRecordWriter') as base, \
            patch('routes.api.FeishuInspectionRecordWriter') as writer:
        source.return_value.read_matching_records.return_value = (
            FeishuRawRecord('rec-device', {config.DEVICE_ID_FIELD: 'TH-10', '区域': 'A'}),
        )
        source.return_value.read_records.return_value = records
        writer.return_value.create_snapshot.return_value = {'record_id': 'fake'}
        payload = {'device_id': 'TH-10', 'area': area,
                   'inspected_at': '2026-10-10T10:00:00+08:00'}
        if parent is not None:
            payload['parent_record_id'] = parent
        response = app.test_client().post('/api/inspections',
                                         headers={'X-History-Key': 'audit-key'}, json=payload)
        assert response.status_code == expected
        if expected == 403:
            writer.assert_not_called()
            base.assert_not_called()


def test_inspection_lookup_failure_denies_write():
    from routes.api import _inspection_scope_error
    app = Flask(__name__)
    source = MagicMock()
    source.read_matching_records.side_effect = RuntimeError('network unavailable')
    with app.app_context():
        assert _inspection_scope_error({'device_id': 'TH-10', 'area': 'A'}, source)[1] == 403


def test_snapshot_effective_time_controls_token_and_lookup():
    writer = MagicMock()
    writer.create.return_value = {'code': 0}
    source = MagicMock()
    source.read_records.return_value = ()
    adapter = FeishuInspectionRecordWriter(writer=writer, source=source,
                                          inspection_table_id='table', device_table_id='devices')
    t1 = datetime.fromisoformat('2026-10-10T10:00:00+08:00')
    t2 = datetime.fromisoformat('2026-10-10T11:00:00+08:00')
    adapter.create_snapshot(area='A', inspected_at=t1, state_recorded_at=t1)
    adapter.create_snapshot(area='A', inspected_at=t1, state_recorded_at=t2)
    calls = writer.create.call_args_list
    assert calls[0].kwargs['client_token'] != calls[1].kwargs['client_token']
    source.read_records.return_value = (
        FeishuRawRecord('existing', calls[1].args[1]),
    )
    result = adapter.create_snapshot(area='A', inspected_at=t1, state_recorded_at=t2)
    assert result['record_id'] == 'existing'
    assert writer.create.call_count == 2
    source.read_records.return_value = ()
    adapter.create_snapshot(area='A', inspected_at=t2, state_recorded_at=t2)
    assert writer.create.call_args.kwargs['client_token'] == calls[1].kwargs['client_token']


@pytest.mark.parametrize('age,ready', [(1, True), (29, True), (30, False), (3600, False)])
def test_readiness_expiry_while_refresh_is_blocked(age, ready):
    original = {'ready': True, 'standards_ready': True, 'active_readiness': True, 'reasons': []}
    with patch.object(bootstrap, '_last_components', object()), \
            patch.object(bootstrap, '_readiness_cache', original), \
            patch.object(bootstrap, '_readiness_cache_at', 100), \
            patch.object(bootstrap, '_readiness_refreshing', True), \
            patch.object(bootstrap.time, 'monotonic', return_value=100 + age), \
            patch.object(bootstrap, 'runtime_liveness', return_value={
                'available': True, 'scheduler_running': True}):
        result = bootstrap.runtime_readiness()
    assert result['ready'] is ready
    assert result['cache_age_seconds'] == age
    assert original['ready'] is True
    if not ready:
        assert result['standards_ready'] is False
        assert 'readiness cache expired' in result['reasons']


def test_readiness_lock_timeout_fails_closed_without_releasing_unowned_lock():
    components = MagicMock()
    components._execution_lock.acquire.return_value = False
    with patch.object(bootstrap, 'runtime_liveness', return_value={'scheduler_running': True}):
        result = bootstrap._compute_runtime_readiness(components)
    assert result['ready'] is False
    assert result['reasons'] == ['readiness execution lock timeout']
    components.status.assert_not_called()
    components._execution_lock.release.assert_not_called()
    components._execution_lock.acquire.assert_called_once_with(timeout=5.0)


@pytest.mark.parametrize('records', [(), (
    FeishuRawRecord('one', {config.DEVICE_ID_FIELD: 'TH-10', '区域': 'A'}),
    FeishuRawRecord('two', {config.DEVICE_ID_FIELD: 'TH-10', '区域': 'A'}),
)])
def test_inspection_missing_or_ambiguous_device_denies_scope(records):
    from routes.api import _inspection_scope_error
    source = MagicMock()
    source.read_matching_records.return_value = records
    with Flask(__name__).app_context():
        assert _inspection_scope_error({'device_id': 'TH-10', 'area': 'A'}, source)[1] == 403


def test_inspection_accepts_feishu_rich_text_region():
    from routes.api import _inspection_scope_error
    source = MagicMock()
    source.read_matching_records.return_value = (
        FeishuRawRecord('device', {config.DEVICE_ID_FIELD: [{'text': 'TH-10'}],
                                  '区域': [{'text': 'PE仓库'}]}),
    )
    source.read_records.return_value = (
        FeishuRawRecord('parent', {'仓库区域': [{'text': 'PE仓库'}]}),
    )
    with Flask(__name__).app_context():
        assert _inspection_scope_error({'device_id': 'TH-10', 'area': 'PE仓库',
                                        'parent_record_id': 'parent'}, source) is None
