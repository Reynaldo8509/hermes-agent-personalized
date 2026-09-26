from .provider import GoogleCSEWebSearchProvider


def register(ctx) -> None:
    ctx.register_web_search_provider(GoogleCSEWebSearchProvider())
