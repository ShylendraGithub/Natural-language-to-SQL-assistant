"""The HTTP client is tested against a local stub server (no real API call is made)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from nl2sql.llm import AnthropicClient, LLMError


class Handler(BaseHTTPRequestHandler):
    status = 200
    payload = {"content": [{"type": "text", "text": "```sql\nSELECT 1\n```"}]}
    captured: dict = {}

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        Handler.captured = {
            "path": self.path,
            "api_key": self.headers.get("x-api-key"),
            "version": self.headers.get("anthropic-version"),
            "content_type": self.headers.get("content-type"),
            "body": json.loads(self.rfile.read(length)),
        }
        data = json.dumps(Handler.payload).encode()
        self.send_response(Handler.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def server(monkeypatch):
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    Handler.status = 200
    Handler.payload = {"content": [{"type": "text", "text": "```sql\nSELECT 1\n```"}]}
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_request_shape_and_response_parsing(server):
    client = AnthropicClient(api_key="k-123", model="some-model", base_url=server, max_tokens=321)
    text = client.complete("be careful", [{"role": "user", "content": "hi"}])
    assert text == "```sql\nSELECT 1\n```"
    got = Handler.captured
    assert got["path"] == "/v1/messages"
    assert got["api_key"] == "k-123"
    assert got["version"] == "2023-06-01"
    assert got["content_type"] == "application/json"
    assert got["body"] == {
        "model": "some-model",
        "max_tokens": 321,
        "system": "be careful",
        "messages": [{"role": "user", "content": "hi"}],
    }  # no temperature unless asked for


def test_temperature_only_sent_when_set(server):
    AnthropicClient(api_key="k", base_url=server, temperature=0.0).complete("s", [{"role": "user", "content": "x"}])
    assert Handler.captured["body"]["temperature"] == 0.0


def test_http_error_becomes_llm_error(server):
    Handler.status = 401
    Handler.payload = {"error": {"message": "invalid x-api-key"}}
    with pytest.raises(LLMError, match="401"):
        AnthropicClient(api_key="bad", base_url=server).complete("s", [{"role": "user", "content": "x"}])


def test_empty_response_becomes_llm_error(server):
    Handler.payload = {"content": []}
    with pytest.raises(LLMError, match="no text"):
        AnthropicClient(api_key="k", base_url=server).complete("s", [{"role": "user", "content": "x"}])


def test_connection_failure_becomes_llm_error():
    client = AnthropicClient(api_key="k", base_url="http://127.0.0.1:9", timeout=2)
    with pytest.raises(LLMError):
        client.complete("s", [{"role": "user", "content": "x"}])


def test_missing_api_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        AnthropicClient()


def test_model_resolution_order(monkeypatch):
    monkeypatch.setenv("NL2SQL_MODEL", "from-env")
    assert AnthropicClient(api_key="k").model == "from-env"
    assert AnthropicClient(api_key="k", model="explicit").model == "explicit"
    monkeypatch.delenv("NL2SQL_MODEL")
    assert AnthropicClient(api_key="k").model == "claude-sonnet-5-5"
