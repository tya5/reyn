from . import SearchResult


class DuckDuckGoBackend:
    name = "duckduckgo"

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        try:
            from ddgs import DDGS
        except ImportError as exc:
            raise RuntimeError(
                "web_search with backend='duckduckgo' requires the 'ddgs' package. "
                "Install with: pip install ddgs"
            ) from exc

        raw = list(DDGS().text(query, max_results=max_results))
        return [
            SearchResult(
                title=hit.get("title", ""),
                url=hit.get("href", ""),
                snippet=hit.get("body", ""),
            )
            for hit in raw
        ]
