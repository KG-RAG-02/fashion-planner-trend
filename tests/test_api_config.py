"""Service config regression; only synthetic credentials and temporary dotenv files."""
import os
from unittest.mock import Mock, patch

import pytest

from fashion_planner_trend.config import ApiSettings, api_settings, load_env
from fashion_planner_trend.api import server

KEYS = ('FASHION_PLANNER_SERVICE_TOKEN', 'TREND_API_HOST', 'TREND_API_PORT', 'TREND_API_MODE')


def test_env_only_service_keys_without_dotenv(tmp_path):
    process = dict(zip(KEYS, ('synthetic', '0.0.0.0', '9876', 'service')))
    with patch.dict(os.environ, process, clear=True):
        env = load_env(tmp_path / 'absent')
    assert all(env[k] == v for k, v in process.items())
    assert api_settings(env) == ApiSettings('0.0.0.0', 9876, 'service')


@pytest.mark.parametrize('value', ['process', ''])
def test_process_overrides_dotenv_including_empty(tmp_path, value):
    dotenv = tmp_path / 'dotenv'
    dotenv.write_text('\n'.join(f'{k}=file' for k in KEYS) + '\nCUSTOM_KEY=file\n')
    process = {k: value for k in KEYS}
    process.update(CUSTOM_KEY=value, UNRELATED_SECRET='excluded', NEO4J_USER='user', LLM_BACKEND='backend')
    with patch.dict(os.environ, process, clear=True):
        env = load_env(dotenv)
    assert all(env[k] == value for k in KEYS)
    assert env['CUSTOM_KEY'] == value
    assert env['NEO4J_USER'] == 'user' and env['LLM_BACKEND'] == 'backend'
    assert 'UNRELATED_SECRET' not in env


def test_dotenv_only_values_and_missing_keys(tmp_path):
    dotenv = tmp_path / 'dotenv'
    dotenv.write_text('# comment\nTREND_API_PORT="9876"\nFASHION_PLANNER_SERVICE_TOKEN=\'synthetic\'\n')
    with patch.dict(os.environ, {}, clear=True):
        env = load_env(dotenv)
        assert load_env(tmp_path / 'absent') == {}
    assert 'TREND_API_HOST' not in env
    assert api_settings(env) == ApiSettings(port=9876)
    assert api_settings({}) == ApiSettings()


def test_empty_values_use_defaults_but_do_not_restore_token():
    env = {k: '' for k in KEYS}
    assert api_settings(env) == ApiSettings()
    with pytest.raises(ValueError, match='requires FASHION_PLANNER'):
        api_settings(env, mode='service', host='0.0.0.0')


def test_cli_precedence():
    env = dict(TREND_API_MODE='service', TREND_API_HOST='0.0.0.0', TREND_API_PORT='9876')
    assert api_settings(env, mode='local', host='127.0.0.1', port=8768) == ApiSettings()


@pytest.mark.parametrize('host', ['0.0.0.0', '192.0.2.1', ''])
def test_local_mode_rejects_nonloopback(host):
    with pytest.raises(ValueError, match='loopback'):
        api_settings({}, host=host)


@pytest.mark.parametrize('host', ['127.0.0.1', 'localhost', '::', '192.0.2.1'])
def test_service_mode_requires_explicit_ipv4_wildcard(host):
    with pytest.raises(ValueError, match='explicit'):
        api_settings({'FASHION_PLANNER_SERVICE_TOKEN': 'synthetic'}, mode='service', host=host)


@pytest.mark.parametrize('token', [None, '', '   '])
def test_service_mode_requires_nonempty_token(token):
    env = {} if token is None else {'FASHION_PLANNER_SERVICE_TOKEN': token}
    with pytest.raises(ValueError, match='requires FASHION_PLANNER'):
        api_settings(env, host='0.0.0.0', mode='service')


@pytest.mark.parametrize('port', [0, -1, 65536, 'bad', 1.5, True])
def test_invalid_ports_are_rejected_without_value_disclosure(port):
    with pytest.raises(ValueError, match='integer between'):
        api_settings({}, port=port)


def test_invalid_mode_is_rejected():
    with pytest.raises(ValueError, match='local or service'):
        api_settings({}, mode='invalid')


def test_rejected_service_settings_do_not_connect():
    with patch.object(server, 'load_env', return_value={}), patch.object(server, 'neo4j_driver') as connect:
        with pytest.raises(SystemExit):
            server.serve('0.0.0.0', mode='service')
    connect.assert_not_called()


@pytest.mark.parametrize('host,mode,server_name', [('127.0.0.1', 'local', 'ThreadingHTTPServer'), ('::1', 'local', '_IPv6HTTPServer'), ('0.0.0.0', 'service', 'ThreadingHTTPServer')])
def test_startup_uses_settings_and_closes_resources(host, mode, server_name):
    driver = Mock()
    http = Mock()
    env = {'FASHION_PLANNER_SERVICE_TOKEN': 'synthetic', 'TREND_API_HOST': host,
           'TREND_API_MODE': mode, 'TREND_API_PORT': '9876'}
    with patch.object(server, 'load_env', return_value=env), patch.object(server, 'neo4j_driver', return_value=driver), patch.object(server, server_name, return_value=http) as factory:
        server.serve()
    assert factory.call_args.args[0] == (host, 9876)
    driver.verify_connectivity.assert_called_once()
    driver.close.assert_called_once()
    http.serve_forever.assert_called_once()
    http.server_close.assert_called_once()


def test_bind_failure_closes_driver():
    driver = Mock()
    with patch.object(server, 'load_env', return_value={}), patch.object(server, 'neo4j_driver', return_value=driver), patch.object(server, 'ThreadingHTTPServer', side_effect=PermissionError):
        with pytest.raises(PermissionError):
            server.serve()
    driver.close.assert_called_once()
