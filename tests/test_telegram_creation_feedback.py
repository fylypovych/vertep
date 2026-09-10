import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize('failure', ['character', 'storage', None])
def test_character_selection_reports_saved_job_or_keeps_retry(monkeypatch, failure):
    module = importlib.import_module('core.app')
    adapter = Mock()
    monkeypatch.setattr(module, 'TelegramAdapter', lambda: adapter)
    pending = {'audit-chat': {'pending': {}, 'brand_id': 'brand01'}}
    monkeypatch.setattr(module, '_telegram_pending_character', pending)
    load = Mock(side_effect=ValueError('invalid') if failure == 'character' else None)
    monkeypatch.setattr(module, 'load_character', load)
    job = SimpleNamespace(job_id='job-audit', model_dump=lambda **kw: {'job_id': 'job-audit'})
    create = Mock(side_effect=OSError('storage unavailable') if failure == 'storage' else None, return_value=job)
    monkeypatch.setattr(module, '_create_job_from_telegram', create)
    queue = Mock()
    monkeypatch.setattr(module, '_queue_storyboard', queue)
    result = module._handle_character_selection({'id': 'callback'}, 'audit-chat', 'did_samogon')
    messages = [call.args[1] for call in adapter.send_message.call_args_list]
    assert adapter.answer_callback.call_count == 1
    if failure:
        assert result == {'status': 'creation_failed'}
        assert 'audit-chat' in pending
        assert any('Не вдалося' in message for message in messages)
        queue.assert_not_called()
    else:
        assert result['job_id'] == 'job-audit'
        assert 'audit-chat' not in pending
        assert any('job-audit' in message and 'збережено' in message for message in messages)
        queue.assert_called_once_with(job, 'audit-chat')
