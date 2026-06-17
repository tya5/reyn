from typing import Protocol


class SearchResult(dict):
    """A single search hit. Keys: title, url, snippet."""


class SearchBackend(Protocol):
    name: str

    def search(self, query: str, max_results: int) -> list[SearchResult]: ...


def get_backend(name: str) -> SearchBackend:
    if name == "duckduckgo":
        from .duckduckgo import DuckDuckGoBackend
        return DuckDuckGoBackend()
    raise ValueError(f"unknown search backend: {name!r}")
