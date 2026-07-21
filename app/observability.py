from __future__ import annotations

import contextvars
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request, Response
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from app.config import Settings
from app.storage import Storage

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)

HTTP_REQUESTS = Counter(
    "autoflow_http_requests_total",
    "HTTP requests handled by AutoFlow",
    ("method", "route", "status"),
)
HTTP_DURATION = Histogram(
    "autoflow_http_request_duration_seconds",
    "HTTP request latency",
    ("method", "route"),
)
TASK_FAILURES = Counter(
    "autoflow_task_failures_total",
    "Workflow jobs that exhausted retry attempts",
    ("target",),
)
RUNNER_CONNECTIONS = Gauge(
    "autoflow_runner_connections",
    "Authenticated Runner WebSocket connections",
)
QUEUE_DEPTH = Gauge(
    "autoflow_job_queue_depth",
    "Persisted jobs by target and status",
    ("target", "status"),
)


class JsonFormatter(logging.Formatter):
    fields = (
        "request_id",
        "method",
        "path",
        "route",
        "status",
        "duration_ms",
        "task_id",
        "runner_id",
        "job_target",
        "attempt",
        "error",
    )

    def format(self, record: logging.LogRecord) -> str:
        span_context = trace.get_current_span().get_span_context()
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None) or request_id_var.get()
        if request_id:
            payload["request_id"] = request_id
        if span_context.is_valid:
            payload["trace_id"] = format(span_context.trace_id, "032x")
            payload["span_id"] = format(span_context.span_id, "016x")
        for field in self.fields:
            value = getattr(record, field, None)
            if value is not None and field not in payload:
                payload[field] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True


def configure_tracing(app: FastAPI, settings: Settings) -> None:
    if getattr(app.state, "tracing_configured", False):
        return
    provider = TracerProvider(
        resource=Resource.create({"service.name": settings.otel_service_name})
    )
    if settings.otel_exporter_otlp_endpoint:
        exporter = OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
    app.state.tracing_configured = True


async def observe_request(request: Request, call_next) -> Response:
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    token = request_id_var.set(request_id)
    started = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        duration = time.perf_counter() - started
        route_object = request.scope.get("route")
        route = getattr(route_object, "path", request.url.path)
        HTTP_REQUESTS.labels(request.method, route, str(status_code)).inc()
        HTTP_DURATION.labels(request.method, route).observe(duration)
        logging.getLogger("autoflow.http").info(
            "http.request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "route": route,
                "status": status_code,
                "duration_ms": round(duration * 1000, 2),
            },
        )
        request_id_var.reset(token)


def metrics_response(storage: Storage) -> Response:
    for target, status, count in storage.job_status_counts():
        QUEUE_DEPTH.labels(target, status).set(count)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
