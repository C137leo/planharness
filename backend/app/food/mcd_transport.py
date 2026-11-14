"""Read-only MCP transport for migration. No order-writing API is exposed here.

Provider descriptions/content remain untrusted data. Only the adapter's reviewed
read operations are callable, and every request is checked against discovered
JSON Schema. This client is separate from the legacy text-based order handlers.
"""
import json
import re
import httpx
from jsonschema import Draft202012Validator

READ_TOOLS = frozenset({"query-nearby-stores", "query-meals", "query-meal-detail",
                        "calculate-price", "query-order", "list-nutrition-foods"})
VERSIONS = {"2025-06-18", "2025-03-26", "2024-11-05"}


class McpProtocolError(RuntimeError):
    pass


class McpHttpError(McpProtocolError):
    def __init__(self,status_code):
        self.status_code=status_code
        super().__init__('mcp_http_'+str(status_code))


class McpAuthenticationError(McpProtocolError):
    pass


class McpBusinessError(McpProtocolError):
    def __init__(self,error_code='mcp_business_rejected',*,provider_code=None,provider_message='',tool=''):
        super().__init__(error_code)
        self.provider_code=provider_code if isinstance(provider_code,(str,int)) else None
        text=' '.join(str(provider_message or '').split())[:240]
        # Provider text is display data, never instructions or a place for secrets.
        text=re.sub(r'(?i)(bearer\s+)[^\s]+',r'\1[redacted]',text)
        text=re.sub(r'(?i)((?:token|authorization|secret|api[_-]?key)\s*[:=]\s*)[^\s,;]+',r'\1[redacted]',text)
        self.provider_message=text
        self.tool=tool


def messages(body):
    """Parse JSON or complete SSE events; never confuse notifications with results."""
    try:
        value = json.loads(body)
        return [value] if isinstance(value, dict) else []
    except ValueError:
        result, event = [], []
        for line in body.splitlines() + [""]:
            if not line:
                if event:
                    try:
                        value = json.loads("\n".join(event))
                        if isinstance(value, dict):
                            result.append(value)
                    except ValueError:
                        raise McpProtocolError("invalid_sse_json") from None
                    event = []
            elif line.startswith("data:"):
                event.append(line[5:].lstrip())
        return result


def _reject_external_references(schema):
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key in {"$ref", "$dynamicRef"} and (not isinstance(value, str) or not value.startswith("#")):
                raise McpProtocolError("external_schema_reference_not_allowed")
            _reject_external_references(value)
    elif isinstance(schema, list):
        for value in schema:
            _reject_external_references(value)


class ReadOnlyMcdClient:
    def __init__(self, *, url, token, timeout=20, http=None):
        if not url.startswith("https://") or not token:
            raise ValueError("https_endpoint_and_token_required")
        self.url, self.timeout = url, timeout
        self.headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream"}
        self.http = http if http is not None else httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False)
        self.owns_http = http is None
        self.tools, self._id, self.initialized = {}, 0, False

    def close(self):
        if self.owns_http:
            self.http.close()

    def _rpc(self, method, params=None, *, notification=False):
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notification:
            self._id += 1
            payload["id"] = self._id
        response = self.http.post(self.url, headers=dict(self.headers), json=payload, timeout=self.timeout)
        if response.status_code in {401, 403}:
            raise McpAuthenticationError("mcp_authentication_failed")
        if response.status_code not in {200, 202, 204}:
            raise McpHttpError(response.status_code)
        if len(response.content) > 2_000_000:
            raise McpProtocolError("mcp_response_too_large")
        session = response.headers.get("mcp-session-id")
        if session:
            self.headers["Mcp-Session-Id"] = session
        if notification:
            return None
        reply = next((m for m in messages(response.text) if m.get("id") == payload["id"] and m.get("jsonrpc") == "2.0"), None)
        if reply is None:
            raise McpProtocolError("missing_matching_rpc_response")
        if "error" in reply:
            raise McpProtocolError("mcp_rpc_error_" + str((reply["error"] or {}).get("code")))
        if not isinstance(reply.get("result"), dict):
            raise McpProtocolError("invalid_rpc_result")
        return reply["result"]

    def discover(self):
        if not self.initialized:
            result = self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "planharness-commerce-readonly", "version": "1.0"}})
            version = result.get("protocolVersion")
            if version not in VERSIONS:
                raise McpProtocolError("unsupported_protocol_version")
            self.headers["MCP-Protocol-Version"] = version
            self._rpc("notifications/initialized", notification=True)
            self.initialized = True
        tools, cursor, seen = {}, None, set()
        for _ in range(10):
            result = self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            rows = result.get("tools")
            if not isinstance(rows, list):
                raise McpProtocolError("invalid_tools_list")
            for tool in rows:
                name, schema = tool.get("name"), tool.get("inputSchema")
                if not isinstance(name, str) or name in tools or not isinstance(schema, dict):
                    raise McpProtocolError("invalid_tool_definition")
                _reject_external_references(schema)
                Draft202012Validator.check_schema(schema)
                tools[name] = tool
            cursor = result.get("nextCursor")
            if not cursor:
                self.tools = tools
                return tools
            if not isinstance(cursor, str) or cursor in seen:
                raise McpProtocolError("invalid_pagination_cursor")
            seen.add(cursor)
        raise McpProtocolError("tool_discovery_page_limit")

    def call_read(self, name, arguments):
        if name not in READ_TOOLS:
            raise PermissionError("unreviewed_or_mutating_tool_not_allowed")
        for attempt in range(2):
            try:return self._call_known(name, arguments)
            except McpHttpError as exc:
                if attempt or exc.status_code not in {502,503,504}:raise

    def _call_known(self, name, arguments):
        if not self.tools:
            self.discover()
        if name not in self.tools:
            raise McpProtocolError("tool_not_advertised")
        validator = Draft202012Validator(self.tools[name]["inputSchema"])
        if not validator.is_valid(arguments):
            raise ValueError("mcp_arguments_do_not_match_discovered_schema")
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        text = ""
        payload = result.get("structuredContent")
        if not isinstance(payload, dict):
            parts = [p.get("text", "") for p in result.get("content", []) if p.get("type") == "text"]
            text, payload = "\n".join(parts), None
            decoder = json.JSONDecoder()
            for offset, char in enumerate(text):
                if char != "{":
                    continue
                try:
                    candidate, _ = decoder.raw_decode(text[offset:])
                    if isinstance(candidate, dict) and ("success" in candidate or "data" in candidate):
                        payload = candidate
                        break
                except ValueError:
                    pass
        if result.get("isError"):
            raise McpBusinessError("mcp_tool_reported_error",provider_code=(payload or {}).get("code"),
                provider_message=(payload or {}).get("message") or (payload or {}).get("msg") or text,tool=name)
        if not isinstance(payload, dict):
            raise McpProtocolError("missing_structured_business_payload")
        if payload.get("success") is False:
            raise McpBusinessError("mcp_business_rejected",provider_code=payload.get("code"),
                provider_message=payload.get("message") or payload.get("msg") or "",tool=name)
        if payload.get("success") is not True or "data" not in payload:
            raise McpProtocolError("business_success_not_confirmed")
        return payload["data"]


class McdCommerceClient(ReadOnlyMcdClient):
    """Only the server checkout service may call this explicit pickup submission."""
    def submit_pickup(self, arguments):
        if (arguments.get("orderType") != 1 or arguments.get("beType") != 1
                or not arguments.get("takeWayCode") or not arguments.get("items")):
            raise ValueError("explicit_pickup_order_required")
        return self._call_known("create-order", arguments)


    def cancel_pickup(self, arguments):
        if not arguments.get('orderId') or arguments.get('cancelReasonCode') not in {'1','2','3','-1'}:
            raise ValueError('explicit_order_cancellation_required')
        return self._call_known('cancel-order',arguments)
