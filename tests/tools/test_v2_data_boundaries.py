"""Native extraction with inert inputs and two explicitly configured loopback providers."""

import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
from threading import Thread

import pytest


@contextmanager
def _provider(label, redirect_destination=None):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, payload["url"], bool(self.headers.get("Authorization"))))
            if payload["url"].endswith("/provider-redirect"):
                self.send_response(307)
                self.send_header("Location", redirect_destination)
                self.end_headers()
                return
            private = payload["url"].endswith("/private-redirect")
            target = "http://private.fixture.test/fixture-path" if private else payload["url"]
            body = json.dumps({"success": True, "data": {
                "markdown": "discarded-fixture-body" if private else label,
                "metadata": {"url": target, "title": "discarded-fixture-title" if private else label},
            }}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", calls
        finally:
            server.shutdown()
            thread.join(timeout=5)


@pytest.fixture
def projects(tmp_path, monkeypatch):
    from agent import secret_scope
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools import url_safety, web_tools

    home = tmp_path / "user-home"
    state = tmp_path / "hermes-state"
    home.mkdir()
    state.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HERMES_HOME", str(state))
    monkeypatch.delenv("HERMES_ALLOW_PRIVATE_URLS", raising=False)

    def resolve(host, port=None):
        if host == "public.fixture.test" or host == "blocked.fixture.test":
            address = "93.184.216.34"
        elif host == "private.fixture.test":
            address = "10.23.0.4"
        else:
            address = host
        return socket.getaddrinfo(address, port, type=socket.SOCK_STREAM, flags=socket.AI_NUMERICHOST)

    # Supply only inert DNS answers. Actual provider HTTP still runs over sockets.
    monkeypatch.setattr(url_safety, "_getaddrinfo", resolve)

    async def extract(project, endpoint, urls):
        path = state / "profiles" / project
        path.mkdir(parents=True, exist_ok=True)
        config = {"web": {"extract_backend": "firecrawl", "keyless_rescue": False},
                  "security": {"allow_private_urls": False, "website_blocklist": {
                      "enabled": True, "domains": ["blocked.fixture.test"],
                  }}}
        (path / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
        scope = secret_scope.set_multiplex_context(True)
        route = set_hermes_home_override(path)
        secrets = secret_scope.set_secret_scope({"FIRECRAWL_API_URL": endpoint}, profile_home=str(path))
        try:
            return json.loads(await web_tools.web_extract_tool(urls))
        finally:
            secret_scope.reset_secret_scope(secrets)
            reset_hermes_home_override(route)
            secret_scope.reset_multiplex_context(scope)

    return extract


def test_native_extraction_uses_each_project_profiles_approved_endpoint_and_cache(projects):
    url = "https://public.fixture.test/same-url"
    with _provider("alpha fixture") as (alpha, alpha_calls), _provider("beta fixture") as (beta, beta_calls):
        async def concurrent_extract():
            return await asyncio.gather(projects("alpha", alpha, [url]), projects("beta", beta, [url]))

        first, second = asyncio.run(concurrent_extract())
        again = asyncio.run(projects("alpha", alpha, [url]))
    assert [r["results"][0]["content"] for r in (first, second, again)] == [
        "alpha fixture", "beta fixture", "alpha fixture",
    ]
    assert alpha_calls == beta_calls == [("/v2/scrape", url, False)]


def test_native_extraction_rejects_private_and_blocklisted_input_and_discards_private_result(projects):
    url = "https://public.fixture.test/private-redirect"
    with _provider("fixture route") as (endpoint, calls):
        result = asyncio.run(projects("alpha", endpoint, [
            "http://private.fixture.test/direct", "https://blocked.fixture.test/direct", url,
        ]))
    direct, blocked, redirected = result["results"]
    assert "private or internal" in direct["error"]
    assert blocked["blocked_by_policy"]["host"] == "blocked.fixture.test"
    assert redirected["url"] == url
    assert "private or internal" in redirected["error"]
    assert redirected["title"] == redirected["content"] == ""
    assert "fixture-path" not in json.dumps(result)
    assert calls == [("/v2/scrape", url, False)]


def test_native_provider_transport_never_follows_an_http_redirect(projects):
    url = "https://public.fixture.test/provider-redirect"
    with _provider("unapproved fixture") as (other, other_calls):
        with _provider("approved fixture", f"{other}/capture") as (endpoint, calls):
            result = asyncio.run(projects("alpha", endpoint, [url]))
    assert "HTTP 307" in result["results"][0]["error"]
    assert calls == [("/v2/scrape", url, False)]
    assert not other_calls
