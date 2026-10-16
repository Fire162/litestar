# pyright: reportUnnecessaryTypeIgnoreComment=false

from typing import Any, cast

import pytest
from _pytest.fixtures import FixtureRequest
from opentelemetry.metrics import get_meter_provider, set_meter_provider
from opentelemetry.sdk.metrics._internal import MeterProvider
from opentelemetry.sdk.metrics._internal.aggregation import (
    ExplicitBucketHistogramAggregation,
)
from opentelemetry.sdk.metrics._internal.export import InMemoryMetricReader
from opentelemetry.sdk.metrics._internal.instrument import Counter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import Span as SdkSpan
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Span

from litestar import WebSocket, get, websocket
from litestar.config.app import AppConfig
from litestar.enums import ScopeType
from litestar.exceptions import http_exceptions
from litestar.params import FromPath
from litestar.plugins.opentelemetry import OpenTelemetryConfig, OpenTelemetryPlugin, get_route_template
from litestar.status_codes import HTTP_200_OK
from litestar.testing import create_test_client
from litestar.types.asgi_types import ASGIApp, Receive, Scope, Send


@pytest.fixture(scope="session")
def resource() -> Resource:
    return Resource(attributes={SERVICE_NAME: "litestar-test"})


@pytest.fixture(scope="session")
def reader() -> InMemoryMetricReader:
    aggregation_last_value = {Counter: ExplicitBucketHistogramAggregation(boundaries=[])}
    return InMemoryMetricReader(preferred_aggregation=aggregation_last_value)  # type: ignore[arg-type]


@pytest.fixture(scope="session")
def meter_provider(resource: Resource, reader: InMemoryMetricReader) -> MeterProvider:
    provider = MeterProvider(resource=resource, metric_readers=[reader])
    set_meter_provider(provider)
    return provider


@pytest.fixture()
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture()
def config(
    resource: Resource, exporter: InMemorySpanExporter, meter_provider: MeterProvider, request: FixtureRequest
) -> OpenTelemetryConfig:
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    meter = get_meter_provider().get_meter(f"litestar-test-{request.node.nodeid}")
    return OpenTelemetryConfig(tracer_provider=tracer_provider, meter=meter)


@pytest.fixture(params=["middleware", "plugin"])
def app_config(request: FixtureRequest, config: OpenTelemetryConfig) -> AppConfig:
    if request.param == "middleware":
        return AppConfig(middleware=[config.middleware])
    return AppConfig(plugins=[OpenTelemetryPlugin(config)])


def test_open_telemetry_middleware_with_http_route(
    app_config: AppConfig,
    reader: InMemoryMetricReader,
    exporter: InMemorySpanExporter,
) -> None:
    @get("/")
    def handler() -> dict:
        return {"hello": "world"}

    with create_test_client(handler, middleware=app_config.middleware, plugins=app_config.plugins) as client:
        response = client.get("/")
        assert response.status_code == HTTP_200_OK
        assert reader.get_metrics_data()

        first_span, second_span, third_span = cast("tuple[SdkSpan, SdkSpan, SdkSpan]", exporter.get_finished_spans())
        assert dict(first_span.attributes) == {"http.status_code": 200, "asgi.event.type": "http.response.start"}  # type: ignore[arg-type]
        assert dict(second_span.attributes) == {"asgi.event.type": "http.response.body"}  # type: ignore[arg-type]
        assert dict(third_span.attributes) == {  # type: ignore[arg-type]
            "http.scheme": "http",
            "http.host": "testserver.local",
            "net.host.port": 80,
            "http.flavor": "1.1",
            "http.target": "/",
            "http.url": "http://testserver.local/",
            "http.method": "GET",
            "http.server_name": "testserver.local",
            "http.user_agent": "testclient",
            "net.peer.ip": "testclient",
            "net.peer.port": 50000,
            "http.route": "/",
            "http.status_code": 200,
        }
        assert third_span.name == "GET /"

        metric_data = reader.get_metrics_data()
        assert metric_data
        assert metric_data.resource_metrics

        resource_metrics = metric_data.resource_metrics[0]
        assert resource_metrics.scope_metrics

        scope_metrics = next(sm for sm in resource_metrics.scope_metrics if sm.scope.name != "opentelemetry-sdk")
        assert scope_metrics.metrics

        request_metric = scope_metrics.metrics[0]
        assert len(list(request_metric.data.data_points)) == 1


def test_open_telemetry_middleware_with_websocket_route(
    app_config: AppConfig,
    reader: InMemoryMetricReader,
    exporter: InMemorySpanExporter,
) -> None:
    @websocket("/")
    async def handler(socket: "WebSocket") -> None:
        await socket.accept()
        await socket.send_json({"hello": "world"})
        await socket.close()

    with (
        create_test_client(handler, middleware=app_config.middleware, plugins=app_config.plugins) as client,
        client.websocket_connect("/") as ws_client,
    ):
        data = ws_client.receive_json()
        assert data == {"hello": "world"}

        first_span, second_span, third_span, fourth_span, fifth_span = cast(
            "tuple[SdkSpan, SdkSpan, SdkSpan, SdkSpan, SdkSpan]", exporter.get_finished_spans()
        )
        assert dict(first_span.attributes) == {"asgi.event.type": "websocket.connect"}  # type: ignore[arg-type]
        assert dict(second_span.attributes) == {"asgi.event.type": "websocket.accept"}  # type: ignore[arg-type]
        assert dict(third_span.attributes) == {"asgi.event.type": "websocket.send", "http.status_code": 200}  # type: ignore[arg-type]
        assert dict(fourth_span.attributes) == {"asgi.event.type": "websocket.close"}  # type: ignore[arg-type]
        assert dict(fifth_span.attributes) == {  # type: ignore[arg-type]
            "http.scheme": "ws",
            "http.host": "testserver.local",
            "net.host.port": 80,
            "http.target": "/",
            "http.url": "ws://testserver.local/",
            "http.server_name": "testserver.local",
            "http.user_agent": "testclient",
            "net.peer.ip": "testclient",
            "net.peer.port": 50000,
            "http.route": "/",
            "http.status_code": 200,
        }


def test_open_telemetry_middleware_handles_route_not_found_under_span_http(
    app_config: AppConfig,
    reader: InMemoryMetricReader,
    exporter: InMemorySpanExporter,
) -> None:
    @get("/")
    def handler() -> dict:
        raise Exception("random Exception")

    with create_test_client(handler, middleware=app_config.middleware, plugins=app_config.plugins) as client:
        response = client.get("/route_that_does_not_exist")
        assert response.status_code

        first_span, second_span, third_span = cast("tuple[SdkSpan, SdkSpan, SdkSpan]", exporter.get_finished_spans())
        assert dict(first_span.attributes) == {  # type: ignore[arg-type]
            "http.status_code": 404,
            "asgi.event.type": "http.response.start",
        }
        assert dict(second_span.attributes) == {"asgi.event.type": "http.response.body"}  # type: ignore[arg-type]
        assert dict(third_span.attributes) == {  # type: ignore[arg-type]
            "http.scheme": "http",
            "http.host": "testserver.local",
            "net.host.port": 80,
            "http.flavor": "1.1",
            "http.target": "/route_that_does_not_exist",
            "http.url": "http://testserver.local/route_that_does_not_exist",
            "http.method": "GET",
            "http.server_name": "testserver.local",
            "http.user_agent": "testclient",
            "net.peer.ip": "testclient",
            "net.peer.port": 50000,
            "http.status_code": 404,
        }
        assert third_span.name == "GET"


def test_open_telemetry_middleware_handles_method_not_allowed_under_span_http(
    app_config: AppConfig,
    reader: InMemoryMetricReader,
    exporter: InMemorySpanExporter,
) -> None:
    @get("/")
    def handler() -> dict:
        raise Exception("random Exception")

    with create_test_client(handler, middleware=app_config.middleware, plugins=app_config.plugins) as client:
        response = client.post("/")
        assert response.status_code

        first_span, second_span, third_span = cast("tuple[SdkSpan, SdkSpan, SdkSpan]", exporter.get_finished_spans())
        assert dict(first_span.attributes) == {  # type: ignore[arg-type]
            "http.status_code": 405,
            "asgi.event.type": "http.response.start",
        }
        assert dict(second_span.attributes) == {"asgi.event.type": "http.response.body"}  # type: ignore[arg-type]
        assert dict(third_span.attributes) == {  # type: ignore[arg-type]
            "http.scheme": "http",
            "http.host": "testserver.local",
            "net.host.port": 80,
            "http.flavor": "1.1",
            "http.target": "/",
            "http.url": "http://testserver.local/",
            "http.method": "POST",
            "http.server_name": "testserver.local",
            "http.user_agent": "testclient",
            "net.peer.ip": "testclient",
            "net.peer.port": 50000,
            "http.status_code": 405,
        }
        assert third_span.name == "POST"


def test_open_telemetry_middleware_handles_errors_caused_on_middleware(
    app_config: AppConfig,
    reader: InMemoryMetricReader,
    exporter: InMemorySpanExporter,
) -> None:
    raise_exception = True

    def middleware_factory(app: ASGIApp) -> ASGIApp:
        async def error_middleware(scope: Scope, receive: Receive, send: Send) -> None:
            if raise_exception:
                raise http_exceptions.NotAuthorizedException()
            await app(scope, receive, send)

        return error_middleware

    @get("/")
    def handler() -> dict:
        raise Exception("random Exception")

    with create_test_client(
        handler, middleware=[middleware_factory, *app_config.middleware], plugins=app_config.plugins
    ) as client:
        response = client.get("/")
        assert response.status_code

        first_span, second_span, third_span = cast("tuple[SdkSpan, SdkSpan, SdkSpan]", exporter.get_finished_spans())
        assert dict(first_span.attributes) == {  # type: ignore[arg-type]
            "http.status_code": 401,
            "asgi.event.type": "http.response.start",
        }
        assert dict(second_span.attributes) == {"asgi.event.type": "http.response.body"}  # type: ignore[arg-type]
        assert dict(third_span.attributes) == {  # type: ignore[arg-type]
            "http.scheme": "http",
            "http.host": "testserver.local",
            "net.host.port": 80,
            "http.flavor": "1.1",
            "http.target": "/",
            "http.url": "http://testserver.local/",
            "http.method": "GET",
            "http.server_name": "testserver.local",
            "http.user_agent": "testclient",
            "net.peer.ip": "testclient",
            "net.peer.port": 50000,
            "http.route": "/",
            "http.status_code": 401,
        }
        assert third_span.name == "GET /"


def test_after_exception_hook_handler_called_on_exception(
    resource: Resource, exporter: InMemorySpanExporter, meter_provider: MeterProvider, request: FixtureRequest
) -> None:
    """Test that after_exception_hook_handler is called when an exception occurs."""
    # Track calls to the hook
    hook_calls: list[tuple[Exception, Scope]] = []

    def after_exception_hook(exc: Exception, scope: Scope) -> None:
        hook_calls.append((exc, scope))

    # Create config with the hook handler
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    meter = get_meter_provider().get_meter(f"litestar-test-{request.node.nodeid}")
    config = OpenTelemetryConfig(
        tracer_provider=tracer_provider, meter=meter, after_exception_hook_handler=after_exception_hook
    )

    test_exception = ValueError("test error")

    @get("/")
    def handler() -> dict:
        raise test_exception

    with create_test_client(handler, plugins=[OpenTelemetryPlugin(config)]) as client:
        response = client.get("/")
        assert response.status_code == 500

        # Verify the hook was called exactly once
        assert len(hook_calls) == 1
        exc, scope = hook_calls[0]

        # Verify the exception is the one we raised
        assert exc is test_exception
        assert isinstance(exc, ValueError)
        assert str(exc) == "test error"

        # Verify the scope is provided
        assert scope is not None
        assert scope["type"] == ScopeType.HTTP
        assert scope["path"] == "/"
        assert scope["method"] == "GET"


def test_after_exception_hook_handler_not_called_on_success(
    resource: Resource, exporter: InMemorySpanExporter, meter_provider: MeterProvider, request: FixtureRequest
) -> None:
    """Test that after_exception_hook_handler is not called when no exception occurs."""
    # Track calls to the hook
    hook_calls: list[tuple[Exception, Scope]] = []

    def after_exception_hook(exc: Exception, scope: Scope) -> None:
        hook_calls.append((exc, scope))

    # Create config with the hook handler
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    meter = get_meter_provider().get_meter(f"litestar-test-{request.node.nodeid}")
    config = OpenTelemetryConfig(
        tracer_provider=tracer_provider, meter=meter, after_exception_hook_handler=after_exception_hook
    )

    @get("/")
    def handler() -> dict:
        return {"success": True}

    with create_test_client(handler, plugins=[OpenTelemetryPlugin(config)]) as client:
        response = client.get("/")
        assert response.status_code == 200

        # Verify the hook was NOT called
        assert len(hook_calls) == 0


def test_exclude_opt_key_no_keyerror_without_route_handler(
    config: OpenTelemetryConfig,
    reader: InMemoryMetricReader,
    exporter: InMemorySpanExporter,
) -> None:
    """Test #4468: should_bypass_middleware does not KeyError when route_handler not in scope.

    OTEL middleware runs as outermost middleware, before the ASGI router populates
    scope["route_handler"]. With exclude_opt_key set, the old code would raise KeyError.
    """
    config.exclude_opt_key = "skip_otel"

    @get("/")
    def handler() -> dict:
        return {"hello": "world"}

    with create_test_client(handler, plugins=[OpenTelemetryPlugin(config)]) as client:
        response = client.get("/")
        assert response.status_code == HTTP_200_OK


def test_client_request_hook_receives_three_params(
    resource: Resource, exporter: InMemorySpanExporter, meter_provider: MeterProvider, request: FixtureRequest
) -> None:
    """Test #4466: client_request_hook receives (span, scope, message) — 3 params, not 2.

    The client_request_hook fires when receive() is called, so we need a POST handler
    that reads the request body to trigger it.
    """
    from litestar import post

    hook_calls: list[tuple] = []

    def client_request_hook(span: "Span", scope: "dict[str, Any]", message: "dict[str, Any]") -> None:
        hook_calls.append((span, scope, message))

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    meter = get_meter_provider().get_meter(f"litestar-test-{request.node.nodeid}")
    config = OpenTelemetryConfig(
        tracer_provider=tracer_provider,
        meter=meter,
        client_request_hook_handler=client_request_hook,
    )

    @post("/")
    async def handler(data: dict) -> dict:
        return data

    with create_test_client(handler, plugins=[OpenTelemetryPlugin(config)]) as client:
        response = client.post("/", json={"key": "value"})
        assert response.status_code == 201

        # The hook fires on receive() calls — with 3 args (span, scope_dict, message_dict)
        assert len(hook_calls) >= 1
        for call in hook_calls:
            assert len(call) == 3
            span, scope_dict, message_dict = call
            assert isinstance(span, Span)
            assert isinstance(scope_dict, dict)
            assert isinstance(message_dict, dict)


def test_client_response_hook_receives_three_params(
    resource: Resource, exporter: InMemorySpanExporter, meter_provider: MeterProvider, request: FixtureRequest
) -> None:
    """Test #4466: client_response_hook receives (span, scope, message) — 3 params, not 2."""
    hook_calls: list[tuple] = []

    def client_response_hook(span: "Span", scope: "dict[str, Any]", message: "dict[str, Any]") -> None:
        hook_calls.append((span, scope, message))

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    meter = get_meter_provider().get_meter(f"litestar-test-{request.node.nodeid}")
    config = OpenTelemetryConfig(
        tracer_provider=tracer_provider,
        meter=meter,
        client_response_hook_handler=client_response_hook,
    )

    @get("/")
    def handler() -> dict:
        return {"hello": "world"}

    with create_test_client(handler, plugins=[OpenTelemetryPlugin(config)]) as client:
        response = client.get("/")
        assert response.status_code == HTTP_200_OK

        assert len(hook_calls) >= 1
        for call in hook_calls:
            assert len(call) == 3
            span, scope_dict, message_dict = call
            assert isinstance(span, Span)
            assert isinstance(scope_dict, dict)
            assert isinstance(message_dict, dict)


def test_server_request_hook_receives_two_params(
    resource: Resource, exporter: InMemorySpanExporter, meter_provider: MeterProvider, request: FixtureRequest
) -> None:
    """Verify server_request_hook still receives (span, scope) — 2 params."""
    hook_calls: list[tuple] = []

    def server_request_hook(span: "Span", scope: "dict[str, Any]") -> None:
        hook_calls.append((span, scope))

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    meter = get_meter_provider().get_meter(f"litestar-test-{request.node.nodeid}")
    config = OpenTelemetryConfig(
        tracer_provider=tracer_provider,
        meter=meter,
        server_request_hook_handler=server_request_hook,
    )

    @get("/")
    def handler() -> dict:
        return {"hello": "world"}

    with create_test_client(handler, plugins=[OpenTelemetryPlugin(config)]) as client:
        response = client.get("/")
        assert response.status_code == HTTP_200_OK

        assert len(hook_calls) == 1
        span, scope_dict = hook_calls[0]
        assert isinstance(span, Span)
        assert isinstance(scope_dict, dict)


def test_new_config_params_accepted() -> None:
    """Test upstream parity: new config params are accepted without error."""
    config = OpenTelemetryConfig(
        http_capture_headers_server_request=["content-type", "x-request-id"],
        http_capture_headers_server_response=["content-type"],
        http_capture_headers_sanitize_fields=["authorization", "cookie"],
    )
    assert config.http_capture_headers_server_request == ["content-type", "x-request-id"]
    assert config.http_capture_headers_server_response == ["content-type"]
    assert config.http_capture_headers_sanitize_fields == ["authorization", "cookie"]


def test_tracer_config_param_accepted() -> None:
    """Test upstream parity: tracer param is accepted."""
    from opentelemetry.trace import get_tracer

    tracer = get_tracer("test")
    config = OpenTelemetryConfig(tracer=tracer)
    assert config.tracer is tracer


def test_open_telemetry_middleware_with_parameterized_route(
    app_config: AppConfig,
    exporter: InMemorySpanExporter,
) -> None:
    @get("/articles/{article_id:int}")
    def handler(article_id: FromPath[int]) -> dict[str, int]:
        return {"id": article_id}

    with create_test_client(handler, middleware=app_config.middleware, plugins=app_config.plugins) as client:
        response = client.get("/articles/9041")
        assert response.status_code == HTTP_200_OK

        _first_span, _second_span, third_span = cast("tuple[SdkSpan, SdkSpan, SdkSpan]", exporter.get_finished_spans())
        assert third_span.name == "GET /articles/{article_id}"
        assert third_span.attributes["http.route"] == "/articles/{article_id}"  # type: ignore[index]


def test_open_telemetry_middleware_with_root_path(
    app_config: AppConfig,
    exporter: InMemorySpanExporter,
) -> None:
    @get("/items/{item_id:str}")
    def handler(item_id: FromPath[str]) -> dict[str, str]:
        return {"id": item_id}

    with create_test_client(
        handler,
        root_path="/api/v1",
        middleware=app_config.middleware,
        plugins=app_config.plugins,
    ) as client:
        response = client.get("/api/v1/items/abc")
        assert response.status_code == HTTP_200_OK

        _first_span, _second_span, third_span = cast("tuple[SdkSpan, SdkSpan, SdkSpan]", exporter.get_finished_spans())
        assert third_span.name == "GET /items/{item_id}"
        assert third_span.attributes["http.route"] == "/items/{item_id}"  # type: ignore[index]


def test_open_telemetry_metrics_with_semconv_opt_in(
    monkeypatch: pytest.MonkeyPatch,
    resource: Resource,
    exporter: InMemorySpanExporter,
    request: FixtureRequest,
) -> None:
    from opentelemetry.instrumentation.asgi import _OpenTelemetrySemanticConventionStability

    monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", "http")
    _OpenTelemetrySemanticConventionStability._initialized = False
    _OpenTelemetrySemanticConventionStability._initialize()

    try:
        reader = InMemoryMetricReader()
        meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
        meter = meter_provider.get_meter(f"litestar-test-{request.node.nodeid}")

        test_config = OpenTelemetryConfig(
            tracer_provider=tracer_provider,
            meter=meter,
            meter_provider=meter_provider,
        )

        @get("/orders/{order_id:int}", sync_to_thread=False)
        def handler(order_id: FromPath[int]) -> dict[str, int]:
            return {"order_id": order_id}

        with create_test_client(handler, plugins=[OpenTelemetryPlugin(test_config)]) as client:
            response = client.get("/orders/42")
            assert response.status_code == HTTP_200_OK

            client.get("/not-found")

            metric_data = reader.get_metrics_data()
            assert metric_data

            duration_routes = set()
            for rm in metric_data.resource_metrics:
                for sm in rm.scope_metrics:
                    for m in sm.metrics:
                        if m.name == "http.server.request.duration":
                            for dp in m.data.data_points:
                                duration_routes.add(dp.attributes.get("http.route"))

            assert "/orders/{order_id}" in duration_routes
            assert "GET /orders/42" not in duration_routes
            assert "/orders/42" not in duration_routes
    finally:
        _OpenTelemetrySemanticConventionStability._initialized = False
        _OpenTelemetrySemanticConventionStability._initialize()


def test_get_route_template_helper() -> None:
    from litestar import Litestar
    from litestar.plugins.opentelemetry import get_route_details_from_scope

    # 1. From scope directly
    assert get_route_template({"path_template": "/existing/{id}"}) == "/existing/{id}"

    # 2. Scope without litestar_app
    assert get_route_template({"path": "/foo", "method": "GET"}) is None

    # 3. Scope with litestar_app and matching route
    @get("/users/{user_id:int}", sync_to_thread=False)
    def user_handler(user_id: FromPath[int]) -> dict[str, int]:
        return {"id": user_id}

    app = Litestar(route_handlers=[user_handler])
    scope = {"type": "http", "method": "GET", "path": "/users/123", "litestar_app": app}
    assert get_route_template(scope) == "/users/{user_id}"

    name, attrs = get_route_details_from_scope(scope)
    assert name == "GET /users/{user_id}"
    assert attrs == {"http.route": "/users/{user_id}"}

    # 4. Scope with 404 (unmatched)
    scope_404 = {"type": "http", "method": "GET", "path": "/not-found", "litestar_app": app}
    assert get_route_template(scope_404) is None
    name_404, attrs_404 = get_route_details_from_scope(scope_404)
    assert name_404 == "GET"
    assert attrs_404 == {}

    # 5. Scope with 405 (method not allowed)
    scope_405 = {"type": "http", "method": "POST", "path": "/users/123", "litestar_app": app}
    assert get_route_template(scope_405) is None
    name_405, attrs_405 = get_route_details_from_scope(scope_405)
    assert name_405 == "POST"
    assert attrs_405 == {}

    # 6. Scope under root_path
    scope_root = {
        "type": "http",
        "method": "GET",
        "root_path": "/api",
        "path": "/api/users/123",
        "litestar_app": app,
    }
    assert get_route_template(scope_root) == "/users/{user_id}"
    name_root, attrs_root = get_route_details_from_scope(scope_root)
    assert name_root == "GET /users/{user_id}"
    assert attrs_root == {"http.route": "/users/{user_id}"}
