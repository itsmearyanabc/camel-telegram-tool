import pytest
import json
from app import app
from unittest.mock import MagicMock

@pytest.fixture
def client():
    app.config['TESTING'] = True
    app.config['SECRET_KEY'] = 'test_key'
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess['logged_in'] = True
        yield client

def test_index_route(client):
    res = client.get('/')
    assert res.status_code == 200

def test_api_session_start_not_found(client, mocker):
    # Mock bot_manager.get_worker to return None
    mocker.patch('api.routes.bot_manager.get_worker', return_value=None)
    
    res = client.post('/api/session/start', data=json.dumps({'phone': '+999'}), content_type='application/json')
    assert res.status_code == 404
    assert res.get_json()['status'] == 'error'

def test_api_session_start_success(client, mocker):
    mock_worker = MagicMock()
    mocker.patch('api.routes.bot_manager.get_worker', return_value=mock_worker)
    mocker.patch('api.routes.update_account_setting')
    
    res = client.post('/api/session/start', data=json.dumps({'phone': '+123'}), content_type='application/json')
    assert res.status_code == 200
    assert res.get_json()['status'] == 'success'
    mock_worker.start.assert_called_once()

def test_api_add_account_duplicate(client, mocker):
    mocker.patch('api.routes.load_config', return_value={'phones': '+123'})
    
    res = client.post('/api/add-account', data=json.dumps({'phone': '+123'}), content_type='application/json')
    assert res.status_code == 409
    assert 'Exists' in res.get_json()['message']
