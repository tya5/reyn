from reyn._network import resolve_env_proxy_url

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

        # #3075 fix 4: ddgs only reads its own DDGS_PROXY env var, not the
        # standard HTTP(S)_PROXY/ALL_PROXY — pass the resolved standard-env
        # proxy explicitly so this egress conforms like every other one.
        raw = list(
            DDGS(proxy=resolve_env_proxy_url("https")).text(
                query, max_results=max_results
            )
        )
        return [
            SearchResult(
                title=hit.get("title", ""),
                url=hit.get("href", ""),
                snippet=hit.get("body", ""),
            )
            for hit in raw
        ]
