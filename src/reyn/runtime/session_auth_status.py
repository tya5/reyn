"""Reyn-managed credential status for `describe_session` (#5012-A).

Scope, permanently (architect ruling, #5012-A — a boundary, not a starting
point to widen later): ONLY credentials reyn itself stores and refreshes
end-to-end — the `reyn auth` device-grant OAuth flow
(`auth.providers` in `reyn.yaml`, `src/reyn/security/secrets/oauth.py`).
A third-party CLI's own auth state (`gh auth status`, `aws configure`,
`gcloud auth`, ...) is OUT OF SCOPE, permanently: owner's standing rule
("does reyn's code grow when the library's/tool's own case count grows?")
disqualifies it outright — a new parser would be needed for every such
tool reyn never asked to integrate with, an ever-growing case count that
is each tool's OWN responsibility to report, not reyn's to take over.
`gh`'s auth is `gh`'s to report; an agent that needs it can run
`gh auth status` itself (`exec` is available). The reporter's real pain
(seeing a `gh` 401 mid-task with nowhere to check auth state up front)
does not go away — but it is not a reyn defect, so reyn does not grow a
parser to paper over it.

Reports ONLY presence + a reason, never the token, refresh token, client
secret, or scopes (architect constraint, #5012-A) — this module's return
shape cannot even represent leaking one; it only ever emits a bool and a
short string.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from reyn.security.secrets.oauth import load_oauth_token


def describe_auth_status(
    auth_config: Any, *, token_store_path: "Path | None" = None,
) -> "dict[str, dict[str, Any]]":
    """*auth_config*: the `ReynConfig.auth` object (duck-typed via
    `.providers`, a `{name: OAuthProviderConfig}` mapping — kept
    duck-typed for the same reason `session_write_scope.py` is: no
    config-package import this module does not otherwise need).

    *token_store_path*: passed straight through to `load_oauth_token` —
    `None` uses the real default (`~/.reyn/oauth_tokens.json`); a test
    passes an isolated `tmp_path` file instead of touching the real
    store, mirroring `load_oauth_token`/`save_oauth_token`'s own
    `path=` parameter.

    Returns ``{provider_name: {"authenticated": bool, "reason": str}}``
    for every provider DECLARED under `auth.providers` — never a
    provider the caller didn't ask about, and never anything beyond
    these two keys per provider."""
    providers = getattr(auth_config, "providers", None) or {}
    result: "dict[str, dict[str, Any]]" = {}
    for name in providers:
        token = load_oauth_token(name, path=token_store_path)
        if token is None:
            result[name] = {
                "authenticated": False,
                "reason": "no token stored — run `reyn auth login` for this provider",
            }
        elif token.is_expired():
            result[name] = {
                "authenticated": False,
                "reason": "token stored but expired — will auto-refresh on next use",
            }
        else:
            result[name] = {"authenticated": True, "reason": "token present and valid"}
    return result
