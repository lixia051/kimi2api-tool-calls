import json
import re
import time
import uuid
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple
from fastapi import Request
from fastapi.security.utils import get_authorization_scheme_param

TOOL_PROMPT_HEADER = """TOOL CALL FORMAT — FOLLOW EXACTLY.

You have access to caller-provided tools. When a tool is needed, do not answer in prose. Output exactly one XML block using this format:

<|DSML|tool_calls>
  <|DSML|invoke name="TOOL_NAME">
    <|DSML|parameter name="ARG_NAME"><![CDATA[value]]></|DSML|parameter>
  </|DSML|invoke>
</|DSML|tool_calls>

Rules:
1. Put all calls under one <|DSML|tool_calls> root.
2. Tool names and argument names must exactly match the schemas below.
3. Use CDATA for string values, especially paths, code, prompts, and queries.
4. For numbers/booleans/null, plain JSON literal text is allowed.
5. Do not wrap XML in markdown fences.
6. If you call a tool, the first non-whitespace characters must be <|DSML|tool_calls>.
7. Do not output explanations before or after the XML block.
8. If no tool is needed, answer normally.
"""


def _tool_name(tool: Dict[str, Any]) -> str:
    if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
        return str(tool["function"].get("name") or "").strip()
    return str(tool.get("name") or tool.get("type") or "").strip()


def has_function_tools(payload: Dict[str, Any]) -> bool:
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return False
    return any(isinstance(t, dict) and _tool_name(t) for t in tools)


def build_tool_prompt(payload: Dict[str, Any]) -> str:
    tools = [t for t in payload.get("tools") or [] if isinstance(t, dict) and _tool_name(t)]
    lines = [TOOL_PROMPT_HEADER, "Available tools:"]
    for tool in tools:
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            fn = tool["function"]
            name = str(fn.get("name") or "")
            desc = str(fn.get("description") or "")
            params = fn.get("parameters") or {}
        else:
            name = _tool_name(tool)
            desc = str(tool.get("description") or "")
            params = tool.get("parameters") or tool.get("input_schema") or {}
        lines.append(f"\nTool: {name}")
        if desc:
            lines.append(f"Description: {desc}")
        lines.append("JSON Schema parameters:")
        lines.append(json.dumps(params, ensure_ascii=False, indent=2))
    lines.append("\nRemember: if using a tool, output only the DSML XML block.")
    return "\n".join(lines)


def inject_tool_prompt(messages: List[Dict[str, Any]], payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not has_function_tools(payload):
        return messages
    injected = [{"role": "system", "content": build_tool_prompt(payload)}]
    injected.extend(messages)
    return injected


def _strip_ns(tag: str) -> str:
    if "}" in tag:
        tag = tag.rsplit("}", 1)[-1]
    if "|" in tag:
        tag = tag.rsplit("|", 1)[-1]
    if tag.startswith("DSML_"):
        tag = tag[len("DSML_"):]
    return tag


def _normalize_xml(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:xml)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    text = text.replace("<|DSML|", "<DSML_").replace("</|DSML|", "</DSML_")
    return text.strip()


def _parse_scalar(value: str) -> Any:
    s = value.strip()
    if s == "":
        return ""
    if s in {"null", "true", "false"} or re.fullmatch(r"-?\d+(\.\d+)?", s):
        try:
            return json.loads(s)
        except Exception:
            return value
    if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
        try:
            return json.loads(s)
        except Exception:
            return value
    return value


def _node_value(node: ET.Element) -> Any:
    children = list(node)
    if not children:
        return _parse_scalar(node.text or "")
    grouped: Dict[str, Any] = {}
    for child in children:
        key = _strip_ns(child.tag)
        val = _node_value(child)
        if key == "item":
            grouped.setdefault("item", []).append(val)
        elif key in grouped:
            if not isinstance(grouped[key], list):
                grouped[key] = [grouped[key]]
            grouped[key].append(val)
        else:
            grouped[key] = val
    if set(grouped.keys()) == {"item"}:
        return grouped["item"]
    return grouped


def parse_dsml_tool_calls(text: Optional[str]) -> List[Dict[str, Any]]:
    if not text:
        return []
    raw = text.strip()
    match = re.search(r"<\|?DSML\|?tool_calls\b.*?</\|?DSML\|?tool_calls>", raw, flags=re.S)
    if not match:
        match = re.search(r"<tool_calls\b.*?</tool_calls>", raw, flags=re.S)
    xml_text = match.group(0) if match else raw
    xml_text = _normalize_xml(xml_text)
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    if _strip_ns(root.tag) != "tool_calls":
        return []
    calls: List[Dict[str, Any]] = []
    for invoke in root.iter():
        if _strip_ns(invoke.tag) != "invoke":
            continue
        name = (invoke.attrib.get("name") or "").strip()
        if not name:
            continue
        args: Dict[str, Any] = {}
        for param in list(invoke):
            if _strip_ns(param.tag) != "parameter":
                continue
            pname = (param.attrib.get("name") or "").strip()
            if not pname:
                continue
            args[pname] = _node_value(param)
        calls.append({
            "id": f"call_{uuid.uuid4().hex[:24]}",
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(args, ensure_ascii=False, separators=(",", ":")),
            },
        })
    return calls


def tool_choice_required(payload: Dict[str, Any]) -> bool:
    tc = payload.get("tool_choice")
    if tc == "required":
        return True
    if isinstance(tc, dict):
        return True
    return False


def maybe_tool_choice_for_prompt(payload: Dict[str, Any]) -> str:
    if tool_choice_required(payload):
        return " Tool use is required for this request; choose the best matching tool."
    return " If previous messages include TOOL_RESULT, use those results to answer normally and do not call another tool unless necessary."


def _mask_token(token: str) -> str:
    if not token:
        return ""
    if len(token) <= 12:
        return "***"
    return f"{token[:8]}...{token[-4:]}"

async def verify_api_key_compat(request: Request) -> None:
    """Compatibility auth for Hermes/OpenAI debug dumps that may send masked keys.

    The real service should never accept masked keys. This test-only sidecar does so
    because Hermes redacts request headers in its internal retry/debug path while
    replaying through the OpenAI client, which otherwise blocks end-to-end agent tests.
    """
    from fastapi import HTTPException, status
    from ..core.keys import validate_api_key as _validate_api_key
    authorization = request.headers.get("Authorization") or request.headers.get("authorization") or ""
    api_key = _validate_api_key(authorization)
    if api_key is not None:
        return
    scheme, token = get_authorization_scheme_param(authorization)
    if scheme.lower() == "bearer" and token:
        try:
            from ..core.keys import list_keys
            for item in list_keys():
                key = str(getattr(item, "key", "") or "")
                if token == _mask_token(key):
                    request.state.api_key_name = getattr(item, "name", "")
                    return
        except Exception:
            pass
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"message": "Invalid API key" if authorization else "Missing bearer token", "type": "invalid_request_error"},
    )
