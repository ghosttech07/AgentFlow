"""webhook_post: POST JSON to an allowlisted URL (e.g. a Slack incoming webhook). external_write, approval
always. The journal's idempotency key is sent as the Idempotency-Key header, so a replay after an unknown
outcome can be de-duplicated by the receiver."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.tools import netguard
from app.tools.base import Tool, ToolContext, ToolError
from app.tools.registry import register_tool


class WebhookArgs(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    payload: dict[str, Any]


class WebhookResult(BaseModel):
    status: int
    response_text: str


@register_tool
class WebhookTool(Tool):
    name = "webhook_post"
    description = "POST a JSON payload to an allowlisted webhook URL (e.g. Slack). Always needs human approval."
    input_model = WebhookArgs
    output_model = WebhookResult
    side_effect = "external_write"
    supports_idempotency_key = True
    timeout_seconds = 20.0

    async def execute(self, args: WebhookArgs, ctx: ToolContext) -> WebhookResult:
        allowed = list(ctx.options.get("allowed_urls", []))
        if not any(args.url.startswith(prefix) for prefix in allowed):
            raise ToolError(f"URL is not in the webhook allowlist ({', '.join(allowed) or 'empty'})")
        resp = await netguard.guarded_request(
            args.url, method="POST", json_body=args.payload, allowed_types=None, max_redirects=0,
            headers={"Idempotency-Key": ctx.idempotency_key, "Content-Type": "application/json"},
            max_bytes=100_000, timeout=float(ctx.options.get("timeout_seconds", 15)),
        )
        if resp.status >= 400:
            raise ToolError(f"webhook returned HTTP {resp.status}", transient=resp.status >= 500)
        return WebhookResult(status=resp.status, response_text=resp.body.decode("utf-8", "replace")[:500])
