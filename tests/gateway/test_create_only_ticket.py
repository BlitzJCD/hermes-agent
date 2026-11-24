"""Native create-only bootstrap enforcement at the receiving authority."""
import asyncio

import pytest

from tests.gateway.test_session_authorities_multiplex import _runner
from tests.gateway.test_ticket_profile_scope import _Transport


def test_create_only_ticket_reaches_authority_without_submit(tmp_path, monkeypatch):
    asyncio.run(_check_create_only_ticket(tmp_path, monkeypatch))


async def _check_create_only_ticket(tmp_path, monkeypatch):
    from gateway.run_api import GatewayRuntimeAPI
    from gateway.run_runtime import initialize_gateway_runtime
    from gateway.runtime_ownership import process_ownership
    from gateway.session_controls import AuthorityConnection
    from hermes_cli import web_server

    user_home = tmp_path / "user-home"
    user_home.mkdir()
    root = tmp_path / "hermes-state"
    root.mkdir(mode=0o700)
    (root / "config.yaml").write_text(
        "model:\n  provider: custom\n  default: create-only-test\n  base_url: http://127.0.0.1:1/v1\n",
        encoding="utf-8",
    )
    homes = [("default", root)]
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("USERPROFILE", str(user_home))
    monkeypatch.setenv("HERMES_HOME", str(root))
    # uv already selected the dependencies for this in-process gateway.
    with monkeypatch.context() as launch:
        launch.setattr("hermes_cli.venv_sync.prepare_launch", lambda *args: None)
        launch.setattr("hermes_cli._early_recovery.recover_if_needed", lambda *args: None)
        launch.setattr("pm.environments.activate_dependencies", lambda *args: None)
        runner = _runner(root, homes)
    process_ownership.reserve([root])
    try:
        await initialize_gateway_runtime(runner)
        runner.session_runtime_descriptor['state'] = 'ready'
        purpose = 'interactive-create-only'
        ticket = runner.session_ticket_store.mint(profile_id=str(root), subject='uid:1', purpose=purpose)
        protocol = 'hermes-gateway-ticket.' + ticket

        async def handle(ws, *, auth_identity, subprotocol, operator):
            authority = ws.scope['hermes.session_authority']
            connection = AuthorityConnection(authority, _Transport(), auth_identity, operator=operator)

            async def call(method, **params):
                return await connection.dispatch({'id': 1, 'method': method, 'params': params})

            try:
                assert connection.actor.capabilities == {'session:create', 'session:read'}
                created = await call('session.create', request_id='create-only',
                                     source='cli', cwd=str(root), toolsets=[])
                assert "result" in created, created
                session_id = created['result']['session_id']
                assert 'result' in await call('session.resume', session_id=session_id)
                rejected = await call('prompt.submit', session_id=session_id,
                                      input_id='create-only-input', text='not admitted')
                assert rejected['error']['message'] == 'permission_denied'
            finally:
                await connection.close()
            await ws.accept(subprotocol=subprotocol)

        async def fallback(*args):
            pytest.fail('native ticket reached the dashboard fallback')

        async def receive():
            return {'type': 'websocket.connect'}

        sent = []

        async def send(message):
            sent.append(message)

        monkeypatch.setattr('tui_gateway.ws.handle_ws', handle)
        monkeypatch.setattr(web_server, '_DASHBOARD_EMBEDDED_CHAT_ENABLED', True)
        scope = {'type': 'websocket', 'path': '/api/ws', 'scheme': 'ws', 'query_string': b'',
                 'client': ('127.0.0.1', 1234), 'server': ('127.0.0.1', 9999),
                 'headers': [(b'sec-websocket-protocol', f'hermes-gateway-v1, {protocol}'.encode())],
                 'subprotocols': ['hermes-gateway-v1', protocol]}
        await GatewayRuntimeAPI(fallback, runner, web_server.app)(scope, receive, send)
        assert sent[-1]['type'] == 'websocket.accept'
    finally:
        for authority in getattr(runner, 'session_authorities', None) or ():
            authority.db.close()
        process_ownership.release(root)
