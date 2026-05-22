import json
import logging
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import Request

from ..kimi import Kimi2API, KimiAPIError, ChatCompletionChunk
from ..kimi.model_catalog import KimiModelSpec

logger = logging.getLogger("kimi2api.api")


def _stream_error_chunk(message: str, error_type: str = "api_error") -> str:
    payload = {
        "error": {
            "message": message,
            "type": error_type,
            "param": None,
            "code": error_type,
        }
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _mark_stream_error(
    request: Optional[Request],
    message: str = "",
    exc: Optional[KimiAPIError] = None,
) -> None:
    if request is not None:
        request.state.stream_error = True
        request.state.stream_error_message = message
        if exc is not None:
            request.state.upstream_status_code = exc.upstream_status_code
            request.state.upstream_error_type = exc.upstream_error_type
            request.state.upstream_retry_after = exc.retry_after or 0.0


def _mark_kimi_account(request: Optional[Request], account: Dict[str, str]) -> None:
    if request is not None:
        request.state.kimi_account_id = account.get("id", "")
        request.state.kimi_account_name = account.get("name", "")


async def _stream_chat_chunks(
    stream: AsyncIterator[ChatCompletionChunk],
    response_model: str,
) -> AsyncIterator[str]:
    from .tool_compat import parse_dsml_tool_calls
    buffered_text = ""
    buffered_chunks: List[Dict[str, Any]] = []
    async for chunk in stream:
        payload = {
            "id": chunk.id,
            "object": chunk.object,
            "created": chunk.created,
            "model": response_model,
            "choices": chunk.choices,
            "system_fingerprint": "fp_kimi2api",
        }
        choice = chunk.choices[0] if chunk.choices else {}
        delta = choice.get("delta", {}) if isinstance(choice, dict) else {}
        if delta.get("content"):
            buffered_text += delta.get("content") or ""
        buffered_chunks.append(payload)

    tool_calls = parse_dsml_tool_calls(buffered_text)
    if tool_calls:
        base = buffered_chunks[0] if buffered_chunks else {
            "id": "chatcmpl-kimi2api-toolcall",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": response_model,
            "system_fingerprint": "fp_kimi2api",
        }
        role_payload = {**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
        yield f"data: {json.dumps(role_payload, ensure_ascii=False)}\n\n"
        for i, call in enumerate(tool_calls):
            delta_call = {
                "index": i,
                "id": call["id"],
                "type": "function",
                "function": call["function"],
            }
            call_payload = {**base, "choices": [{"index": 0, "delta": {"tool_calls": [delta_call]}, "finish_reason": None}]}
            yield f"data: {json.dumps(call_payload, ensure_ascii=False)}\n\n"
        finish_payload = {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}
        yield f"data: {json.dumps(finish_payload, ensure_ascii=False)}\n\n"
    else:
        for payload in buffered_chunks:
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"

async def _stream_responses_chunks(
    stream: AsyncIterator[ChatCompletionChunk],
) -> AsyncIterator[str]:
    async for chunk in stream:
        delta = chunk.choices[0].get("delta", {})
        event: Dict[str, Any] = {
            "type": "response.output_text.delta",
            "sequence_number": 0,
            "item_id": f"msg_{chunk.id}",
            "output_index": 0,
            "content_index": 0,
            "delta": delta.get("content", ""),
        }

        if delta.get("reasoning_content"):
            event = {
                "type": "response.reasoning.delta",
                "sequence_number": 0,
                "item_id": f"msg_{chunk.id}",
                "output_index": 0,
                "content_index": 0,
                "delta": delta["reasoning_content"],
            }
        elif delta.get("role"):
            continue
        elif chunk.choices[0].get("finish_reason"):
            event = {"type": "response.completed", "response": {"id": chunk.id}}

        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


async def _create_streaming_chat_response(
    *,
    request: Optional[Request] = None,
    model: str,
    model_spec: KimiModelSpec,
    response_model: str,
    messages: List[Dict[str, Any]],
    conversation_id: Optional[str],
    enable_web_search: bool,
) -> AsyncIterator[str]:
    client: Optional[Kimi2API] = None
    try:
        client = Kimi2API(on_account_used=lambda account: _mark_kimi_account(request, account))
        stream = await client.chat.completions.create(
            model=model,
            model_spec=model_spec,
            messages=messages,
            stream=True,
            conversation_id=conversation_id,
            enable_web_search=enable_web_search,
        )
        async for chunk in _stream_chat_chunks(stream, response_model):
            yield chunk
    except KimiAPIError as exc:
        _mark_stream_error(request, str(exc), exc)
        logger.warning("Streaming chat request failed: %s", exc)
        yield _stream_error_chunk(str(exc))
        yield "data: [DONE]\n\n"
    except Exception:
        _mark_stream_error(request, "Streaming request failed")
        logger.exception("Unexpected streaming chat request failure")
        yield _stream_error_chunk("Streaming request failed")
        yield "data: [DONE]\n\n"
    finally:
        if client is not None:
            await client.close()


async def _create_streaming_responses_response(
    *,
    request: Optional[Request] = None,
    model: str,
    model_spec: KimiModelSpec,
    response_model: str,
    messages: List[Dict[str, Any]],
    conversation_id: Optional[str],
    enable_web_search: bool,
) -> AsyncIterator[str]:
    client: Optional[Kimi2API] = None
    try:
        client = Kimi2API(on_account_used=lambda account: _mark_kimi_account(request, account))
        stream = await client.chat.completions.create(
            model=model,
            model_spec=model_spec,
            messages=messages,
            stream=True,
            conversation_id=conversation_id,
            enable_web_search=enable_web_search,
        )
        async for chunk in _stream_responses_chunks(stream):
            yield chunk
    except KimiAPIError as exc:
        _mark_stream_error(request, str(exc), exc)
        logger.warning("Streaming responses request failed: %s", exc)
        yield _stream_error_chunk(str(exc))
        yield "data: [DONE]\n\n"
    except Exception:
        _mark_stream_error(request, "Streaming request failed")
        logger.exception("Unexpected streaming responses request failure")
        yield _stream_error_chunk("Streaming request failed")
        yield "data: [DONE]\n\n"
    finally:
        if client is not None:
            await client.close()
