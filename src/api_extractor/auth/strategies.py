"""The five shipped auth strategies.

Every secret arrives as an `env:` reference and is resolved here, at the point of use —
never baked into the parsed config. Acquired tokens are registered with the log scrubber,
because a `login_token` bearer never passes through the environment and is the single most
likely thing to leak.
"""

from __future__ import annotations

import base64
from typing import Any

from jsonpath_ng.ext import parse as parse_jsonpath

from api_extractor.auth.registry import BoundSender, strategy
from api_extractor.config.loader import resolve_env_tree
from api_extractor.config.models import (
    BasicAuth,
    BearerAuth,
    HeaderAuth,
    LoginTokenAuth,
    OAuthClientCredentialsAuth,
)
from api_extractor.http.client import Request
from api_extractor.logs import register_secret


def extract(body: Any, json_path: str, *, where: str) -> str:
    matches = [match.value for match in parse_jsonpath(json_path).find(body)]
    if not matches or matches[0] is None:
        raise RuntimeError(f"{where}: no token at {json_path} in the response")
    token = str(matches[0])
    register_secret(token)
    return token


@strategy("basic")
def basic(auth: BasicAuth, sender: BoundSender) -> dict[str, str]:
    username = resolve_env_tree(auth.username)
    password = resolve_env_tree(auth.password)
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    register_secret(encoded)
    return {"Authorization": f"Basic {encoded}"}


@strategy("bearer")
def bearer(auth: BearerAuth, sender: BoundSender) -> dict[str, str]:
    token = resolve_env_tree(auth.token)
    return {auth.apply.header: auth.apply.template.format(token=token)}


@strategy("header")
def header(auth: HeaderAuth, sender: BoundSender) -> dict[str, str]:
    return {
        name: entry.template.format(value=resolve_env_tree(entry.value))
        for name, entry in auth.headers.items()
    }


@strategy("login_token")
def login_token(auth: LoginTokenAuth, sender: BoundSender) -> dict[str, str]:
    spec = auth.request
    response = sender.send(
        Request(
            method=spec.method,
            url=sender.url(spec.path),
            query=resolve_env_tree(dict(spec.query)),
            payload=resolve_env_tree(spec.payload),
            headers=resolve_env_tree(dict(spec.headers)),
        )
    )
    if response.status >= 400:
        raise RuntimeError(f"login_token: {spec.path} returned {response.status}")
    token = extract(response.body, auth.token_path, where="login_token")
    return {auth.apply.header: auth.apply.template.format(token=token)}


@strategy("oauth_client_credentials")
def oauth_client_credentials(
    auth: OAuthClientCredentialsAuth, sender: BoundSender
) -> dict[str, str]:
    payload: dict[str, str] = {
        "grant_type": "client_credentials",
        "client_id": resolve_env_tree(auth.client_id),
        "client_secret": resolve_env_tree(auth.client_secret),
    }
    if auth.scope is not None:
        payload["scope"] = auth.scope
    response = sender.send(Request(method="POST", url=auth.token_url, payload=payload))
    if response.status >= 400:
        raise RuntimeError(f"oauth_client_credentials: token endpoint returned {response.status}")
    token = extract(response.body, auth.token_path, where="oauth_client_credentials")
    return {auth.apply.header: auth.apply.template.format(token=token)}
