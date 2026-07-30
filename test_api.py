"""
FastAPI Integration Tests for Hermes App API.

Tests the FastAPI endpoints (models, config, health, streaming/non-streaming completions)
by mocking the underlying AIAgent execution to avoid slow and expensive model API requests.
"""

import json
import os
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

# Configure mock API key for server testing
os.environ["API_SERVER_KEY"] = "test-secret-key"

from hermes_app_api import app

client = TestClient(app)


def test_health():
    """Verify that the health check endpoint returns 200 OK and expected structure."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "startup_model" in data
    assert "models_with_keys" in data


def test_unauthorized():
    """Verify that protected API endpoints return 401 Unauthorized when auth header is missing/invalid."""
    for path in ["/v1/models", "/v1/config"]:
        response = client.get(path)
        assert response.status_code == 401
        assert "Invalid or missing API key" in response.json()["error"]["message"]

    response = client.post("/v1/chat/completions", json={})
    assert response.status_code == 401


def test_list_models():
    """Verify list models returns proper format and lists ready models."""
    response = client.get("/v1/models", headers={"Authorization": "Bearer test-secret-key"})
    assert response.status_code == 200
    data = response.json()
    assert data["object"] == "list"
    assert isinstance(data["data"], list)
    assert len(data["data"]) > 0

    # Verify formatting match for OpenAI /v1/models specification
    for m in data["data"]:
        assert "id" in m
        assert "object" in m
        assert m["object"] == "model"
        assert "owned_by" in m
        assert "ready" in m


def test_get_config():
    """Verify config endpoint returns server settings properly."""
    response = client.get("/v1/config", headers={"Authorization": "Bearer test-secret-key"})
    assert response.status_code == 200
    data = response.json()
    assert "max_iterations" in data
    assert "toolsets" in data
    assert data["auth_enabled"] is True


@patch("run_agent.AIAgent")
def test_chat_completions_non_streaming(mock_agent_class):
    """Test non-streaming /v1/chat/completions with mock AIAgent response."""
    mock_agent = MagicMock()
    mock_agent.run_conversation.return_value = {
        "completed": True,
        "final_response": "Hello, I am Hermes, your coding assistant!",
        "prompt_tokens": 12,
        "completion_tokens": 18,
        "total_tokens": 30
    }
    mock_agent_class.return_value = mock_agent

    payload = {
        "model": "gemini-2.5-flash",
        "messages": [
            {"role": "user", "content": "Hello, who are you?"}
        ],
        "stream": False
    }

    response = client.post(
        "/v1/chat/completions",
        json=payload,
        headers={"Authorization": "Bearer test-secret-key"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["content"] == "Hello, I am Hermes, your coding assistant!"
    assert data["model"] == "gemini-2.5-flash"
    assert data["usage"]["total_tokens"] == 30

    # Verify custom tracing and routing headers
    assert response.headers["X-Hermes-Model-Used"] == "gemini-2.5-flash"
    assert "X-Hermes-Request-Id" in response.headers


@patch("run_agent.AIAgent")
def test_chat_completions_streaming(mock_agent_class):
    """Test streaming /v1/chat/completions with SSE response formatting."""
    mock_agent = MagicMock()

    def mock_run_conversation(*args, **kwargs):
        init_kwargs = mock_agent_class.call_args[1]
        callback = init_kwargs.get("stream_delta_callback")
        if callback:
            callback("Streaming")
            callback(" responses")
            callback(" work")
            callback(" correctly")
        return {"completed": True, "final_response": "Streaming responses work correctly"}

    mock_agent.run_conversation.side_effect = mock_run_conversation
    mock_agent_class.return_value = mock_agent

    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [
            {"role": "user", "content": "Say hello in stream"}
        ],
        "stream": True
    }

    response = client.post(
        "/v1/chat/completions",
        json=payload,
        headers={"Authorization": "Bearer test-secret-key"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"

    # Read SSE events line by line
    lines = response.text.split("\n")
    events = [line for line in lines if line.startswith("data:")]
    assert len(events) > 0

    # First event should yield the role
    first_event = json.loads(events[0][5:])
    assert first_event["choices"][0]["delta"]["role"] == "assistant"

    # Reconstruct streamed tokens
    chunks = []
    for event in events[1:]:
        data_str = event[5:].strip()
        if data_str == "[DONE]":
            break
        data = json.loads(data_str)
        content_delta = data["choices"][0]["delta"].get("content", "")
        if content_delta:
            chunks.append(content_delta)

    full_output = "".join(chunks)
    assert full_output == "Streaming responses work correctly"
