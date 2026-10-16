from typing import TYPE_CHECKING, Any

from litestar.exceptions import MissingDependencyException

__all__ = (
    "get_route_details_from_scope",
    "get_route_template",
)


try:
    import opentelemetry  # noqa: F401
except ImportError as e:
    raise MissingDependencyException("opentelemetry") from e

from opentelemetry.semconv.attributes.http_attributes import HTTP_ROUTE

if TYPE_CHECKING:
    from litestar.types import Scope


def get_route_template(scope: "Scope") -> "str | None":
    """Retrieve the matched route template from an ASGI scope.

    Args:
        scope: The ASGI scope instance.

    Returns:
        The matched route template string, or None if no route matched.
    """
    if (template := scope.get("path_template")) is not None:
        return template

    litestar_app = scope.get("litestar_app") or scope.get("app")
    if litestar_app is not None and hasattr(litestar_app, "asgi_router"):
        return litestar_app.asgi_router.resolve_route_template_from_scope(scope)  # type: ignore[no-any-return]

    return None


def get_route_details_from_scope(scope: "Scope") -> "tuple[str, dict[Any, str]]":
    """Retrieve the span name and attributes from the ASGI scope.

    Args:
        scope: The ASGI scope instance.

    Returns:
        A tuple of the span name and a dict of attrs.
    """
    method = str(scope.get("method", "")).strip()
    template = get_route_template(scope)

    if template is not None:
        span_name = f"{method} {template}" if method else template
        return span_name, {HTTP_ROUTE: template}

    span_name = method or scope.get("path", "").strip() or "websocket"
    return span_name, {}
