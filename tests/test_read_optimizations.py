from unittest.mock import Mock, patch

import pytest

import config
from integrations.feishu_records import FeishuBitableRecordSource, FeishuRawRecord
from integrations.feishu_notifications import FeishuNotificationError, FeishuNotificationWriter
from services import feishu


def test_event_query_filters_remotely_and_projects_closure_fields():
    source = FeishuBitableRecordSource()
    with patch.object(feishu, 'list_bitable_records', return_value=[{'record_id':'r1','fields':{'监测点':'TH-01','闭环状态':'已闭环'}}]) as read:
        result = source.read_device_events('table', 'TH-01', device_field='监测点', max_attempts=1, timeout=3)
    assert result[0].record_id == 'r1'
    assert read.call_args.kwargs == {
        'field_names':['监测点','闭环状态'],
        'record_filter':{'conjunction':'and','conditions':[{'field_name':'监测点','operator':'is','value':['TH-01']}]},
        'max_attempts':1,'timeout':3,
    }


def test_injected_source_keeps_old_contract():
    read = Mock(return_value=[{'record_id':'r1','fields':{}}])
    source = FeishuBitableRecordSource(fetch_records=read)
    assert source.read_device_events('table','TH-01',device_field='监测点')[0].record_id == 'r1'
    read.assert_called_once_with('table')


def test_closure_lookup_keeps_local_shortcut_and_fails_closed():
    source, repository = Mock(), Mock()
    repository.list_active.return_value = ['local-event']
    writer = FeishuNotificationWriter(sender=Mock(), source=source, event_table_id='events', device_table_id='devices', event_repository=repository, attempt_timeout=3)
    assert writer.has_unclosed_event('th-01')
    source.read_device_events.assert_not_called()
    repository.list_active.return_value = []
    source.read_device_events.return_value = [FeishuRawRecord('r', {config.FEISHU_EVENT_DEVICE_FIELD:'TH-01','闭环状态':'已闭环'})]
    assert not writer.has_unclosed_event('th-01')
    source.read_device_events.return_value = [FeishuRawRecord('r', {config.FEISHU_EVENT_DEVICE_FIELD:'TH-01'})]
    assert writer.has_unclosed_event('th-01')
    source.read_device_events.side_effect = RuntimeError('injected')
    with pytest.raises(FeishuNotificationError) as caught:
        writer.has_unclosed_event('th-01')
    assert caught.value.retryable
    assert source.read_device_events.call_args.kwargs['max_attempts'] == 1


def test_filtered_pagination_preserves_filter_and_rejects_repeated_tokens(monkeypatch):
    monkeypatch.setattr(config,'APP_TOKEN','test')
    record_filter = {'conjunction':'and','conditions':[{'field_name':'监测点','operator':'is','value':['TH-01']}]}
    responses = [{'code':0,'data':{'items':[],'has_more':True,'page_token':'same'}}]*2
    with patch.object(feishu,'_request_bitable_json',side_effect=responses) as read:
        with pytest.raises(RuntimeError, match='重复'):
            feishu.list_bitable_records('table', record_filter=record_filter, field_names=['监测点'], max_attempts=1, timeout=3)
    assert read.call_count == 2
    assert all(call.kwargs['json_data']['filter'] == record_filter for call in read.call_args_list)
    assert all(call.kwargs['max_attempts'] == 1 for call in read.call_args_list)


@pytest.mark.parametrize('attempted',[False,True])
def test_deferred_log_warns_only_after_external_failure(tmp_path,monkeypatch,attempted):
    from services import db
    monkeypatch.setattr(config,'SQLITE_ENABLED',False)
    from app import create_app
    db.close()
    monkeypatch.setattr(db,'_init_failed',False)
    monkeypatch.setattr(config,'SQLITE_ENABLED',True)
    monkeypatch.setattr(config,'SQLITE_DB_PATH',tmp_path/'logging.db')
    monkeypatch.setattr(config,'TEMPERATURE_API_KEY','')
    monkeypatch.setattr(config,'DEVICE_NAME_MAP',{})
    monkeypatch.setattr(config,'DEVICES',{})
    monkeypatch.setattr(config,'FEISHU_PROJECTION_INLINE_ENABLED',attempted)
    monkeypatch.setattr(config,'FEISHU_PROJECTION_INLINE_SUPPRESS_SECONDS',0)
    try:
        with patch('routes.temperature.logger') as logger, patch('routes.temperature.resolve_record_id',side_effect=RuntimeError('injected failure')), patch('routes.temperature.save_history'):
            response=create_app().test_client().post('/temperature',json={'device':'TEST-LOG','temperature':24,'humidity':50})
        assert response.status_code == 200
        selected = logger.warning if attempted else logger.info
        other = logger.info if attempted else logger.warning
        assert sum('sample_accepted_projection_deferred' in call.args[0] for call in selected.call_args_list) == 1
        assert not any('sample_accepted_projection_deferred' in call.args[0] for call in other.call_args_list)
    finally:
        db.close()


def test_shadow_observation_filters_both_tables():
    from integrations.feishu_observation import FeishuBitableObservationSource
    source=Mock()
    source.read_matching_records.side_effect=[
        [FeishuRawRecord('device',{'设备编号':'TH-01'})],
        [FeishuRawRecord('event',{'监测点':'TH-01','闭环状态':'已闭环'})],
    ]
    observer=FeishuBitableObservationSource(source=source,device_table_id='devices',event_table_id='events')
    result=observer.read('th-01')
    assert not result['__event_exists']
    assert source.read_matching_records.call_count==2
    source.read_records.assert_not_called()
