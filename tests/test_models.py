from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from api_extractor.config.loader import iter_env_refs, load_source
from api_extractor.config.models import Endpoint, FromMarker, Source, placeholders
from tests.conftest import MINIMAL, REFERENCE_NAME, REFERENCE_SOURCE


def endpoint(**overrides) -> Endpoint:
    return Endpoint.model_validate({"method": "GET", "path": "/x", **overrides})


def test_reference_source_parses():
    source = load_source(REFERENCE_SOURCE)
    assert source.source == REFERENCE_NAME
    assert set(source.endpoints) == {"tenant_info", "alarms", "assets", "measures"}
    assert source.defaults.rate_limit == 5
    assert source.endpoints["assets"].paginate is not None
    assert source.endpoints["assets"].paginate.style == "page_number"


def test_unknown_key_is_rejected(write_source):
    data = {**MINIMAL, "schedule": "daily"}
    with pytest.raises(ValidationError, match="schedule"):
        load_source(write_source(data))


def test_unknown_endpoint_key_is_rejected():
    with pytest.raises(ValidationError, match="depends_on"):
        endpoint(depends_on=["other"])


def test_markers_resolve_at_any_depth():
    ep = endpoint(
        payload={
            "assetType": {"from": "types"},
            "nested": {"deep": [{"value": {"from": "ids"}}]},
        }
    )
    assert isinstance(ep.payload["assetType"], FromMarker)
    assert isinstance(ep.payload["nested"]["deep"][0]["value"], FromMarker)
    locs = {ref.loc for ref in ep.markers()}
    assert locs == {"payload.assetType", "payload.nested.deep[0].value"}


def test_param_name_is_the_nearest_enclosing_key():
    ep = endpoint(query={"type": {"from": "types"}})
    (ref,) = ep.markers()
    assert ref.param == "type"


def test_as_overrides_a_useless_leaf_key():
    ep = endpoint(payload={"filters": [{"key": "assetType", "value": {"from": "types"}}]})
    (ref,) = ep.markers()
    assert ref.param == "value"

    ep = endpoint(
        payload={"filters": [{"key": "assetType", "value": {"from": "types", "as": "assetType"}}]}
    )
    (ref,) = ep.markers()
    assert ref.param == "assetType"


def test_list_element_keeps_the_parent_key():
    ep = endpoint(payload={"values": [{"from": "types"}]})
    (ref,) = ep.markers()
    assert ref.param == "values"


def test_dict_with_extra_keys_stays_literal():
    """A date range is not a marker just because it has a `from`."""
    ep = endpoint(query={"from": "2026-01-01", "to": "2026-02-01"})
    assert ep.markers() == []
    assert ep.query == {"from": "2026-01-01", "to": "2026-02-01"}


def test_bind_rejects_as():
    with pytest.raises(ValidationError, match="`as` is not allowed in bind"):
        endpoint(path="/x/{id}", bind={"id": {"from": "ids", "as": "other"}})


def test_bind_marker_is_typed():
    ep = endpoint(path="/x/{id}", bind={"id": {"from": "ids"}})
    (ref,) = ep.markers()
    assert ref.marker.provider == "ids"
    assert ref.param == "id"


def test_paginate_cursor_lands_in_a_query_param():
    ep = endpoint(paginate={"style": "page_number", "at": "query.page", "has_more": "$.has_next"})
    assert ep.paginate is not None
    assert (ep.paginate.at_root, ep.paginate.at_keys) == ("query", ("page",))
    assert ep.paginate.start == 0


def test_paginate_cursor_lands_at_any_depth_in_the_payload():
    ep = endpoint(
        method="POST",
        payload={"pageLink": {"pageSize": 100}},
        paginate={"style": "page_number", "at": "payload.pageLink.page", "start": 1},
    )
    assert ep.paginate is not None
    assert (ep.paginate.at_root, ep.paginate.at_keys) == ("payload", ("pageLink", "page"))
    assert ep.paginate.start == 1
    assert ep.paginate.has_more is None


@pytest.mark.parametrize(
    "at", ["page", "query", "query.", ".query.page", "payload..page", "body.page", ""]
)
def test_paginate_at_must_be_a_rooted_dotted_path(at):
    with pytest.raises(ValidationError, match="dotted path rooted"):
        endpoint(paginate={"style": "page_number", "at": at})


def test_unshipped_pagination_styles_are_rejected():
    """Only page_number ships. Adding another is a new Literal member plus a strategy."""
    for style in ("offset", "cursor", "link_header"):
        with pytest.raises(ValidationError, match="style"):
            endpoint(paginate={"style": style, "at": "query.page"})


def test_page_size_is_not_a_pagination_key():
    with pytest.raises(ValidationError, match="size"):
        endpoint(paginate={"style": "page_number", "at": "query.page", "size": 100})


def test_absent_limit_inherits_but_explicit_null_does_not():
    assert endpoint().inherits_limit() is True
    assert endpoint(limit=None).inherits_limit() is False
    assert endpoint(limit=5).inherits_limit() is False


def test_bad_fan_out_is_rejected():
    with pytest.raises(ValidationError, match="fan_out"):
        endpoint(fan_out="cartesian")


def test_auth_is_discriminated_on_type():
    data = {
        **MINIMAL,
        "auth": {"type": "bearer", "token": "env:TOKEN"},
    }
    source = Source.model_validate(data)
    assert source.auth is not None
    assert source.auth.apply.header == "Authorization"

    with pytest.raises(ValidationError, match="union_tag_invalid|type"):
        Source.model_validate({**MINIMAL, "auth": {"type": "magic"}})


def test_basic_auth_shape():
    """The reference source authenticates every request with Basic — no login, no token."""
    source = Source.model_validate(yaml.safe_load(REFERENCE_SOURCE.read_text(encoding="utf-8")))
    assert source.auth is not None
    assert source.auth.type == "basic"
    assert (source.auth.username, source.auth.password) == ("env:TB_USER", "env:TB_PASSWORD")


def auth_source(auth: dict) -> Source:
    return Source.model_validate({**MINIMAL, "auth": auth})


def test_header_auth_shorthand():
    """`X-API-Key: env:K` is shorthand for a value with no template."""
    source = auth_source({"type": "header", "headers": {"X-API-Key": "env:TB_API_KEY"}})
    assert source.auth is not None
    entry = source.auth.headers["X-API-Key"]
    assert (entry.value, entry.template) == ("env:TB_API_KEY", "{value}")


def test_header_auth_carries_two_headers_one_templated():
    source = auth_source(
        {
            "type": "header",
            "headers": {
                "X-Authorization": {"value": "env:EDF_API_KEY", "template": "ApiKey {value}"},
                "X-EDF-APIKey": "env:ONEAPI_API_KEY",
            },
        }
    )
    assert source.auth is not None
    headers = source.auth.headers
    assert headers["X-Authorization"].template == "ApiKey {value}"
    assert headers["X-EDF-APIKey"].template == "{value}"
    assert headers["X-EDF-APIKey"].value == "env:ONEAPI_API_KEY"


def test_header_template_must_name_the_secret():
    """`ApiKey {API_KEY}` is the natural first guess and would drop the secret silently."""
    with pytest.raises(ValidationError, match=r"unknown placeholder\(s\) \['API_KEY'\]"):
        auth_source(
            {
                "type": "header",
                "headers": {"X-Authorization": {"value": "env:K", "template": "ApiKey {API_KEY}"}},
            }
        )

    with pytest.raises(ValidationError, match="does not contain"):
        auth_source(
            {"type": "header", "headers": {"X-Auth": {"value": "env:K", "template": "ApiKey"}}}
        )


def test_header_auth_needs_at_least_one_header():
    with pytest.raises(ValidationError):
        auth_source({"type": "header", "headers": {}})


def test_bearer_apply_template_must_name_the_token():
    with pytest.raises(ValidationError, match=r"unknown placeholder\(s\) \['secret'\]"):
        auth_source({"type": "bearer", "token": "env:T", "apply": {"template": "Bearer {secret}"}})


def test_header_auth_env_refs_are_discoverable():
    """Whatever shape the header takes, the env: check must still see the var."""
    source = auth_source(
        {
            "type": "header",
            "headers": {
                "X-Authorization": {"value": "env:EDF_API_KEY", "template": "ApiKey {value}"},
                "X-EDF-APIKey": "env:ONEAPI_API_KEY",
            },
        }
    )
    assert dict(iter_env_refs(source)) == {
        "auth.headers.X-Authorization.value": "EDF_API_KEY",
        "auth.headers.X-EDF-APIKey.value": "ONEAPI_API_KEY",
    }


def test_basic_auth_requires_both_halves():
    with pytest.raises(ValidationError, match="password"):
        Source.model_validate({**MINIMAL, "auth": {"type": "basic", "username": "env:U"}})


def test_login_token_auth_shape():
    """Still a shipped strategy per §7, even though no source uses it yet."""
    source = Source.model_validate(
        {
            **MINIMAL,
            "auth": {
                "type": "login_token",
                "request": {"method": "POST", "path": "/auth/login", "payload": {"u": "env:U"}},
                "token_path": "$.token",
                "apply": {"header": "X-Authorization", "template": "Bearer {token}"},
            },
        }
    )
    assert source.auth is not None
    assert source.auth.type == "login_token"
    assert source.auth.request.path == "/auth/login"
    assert source.auth.apply.header == "X-Authorization"


def test_endpoints_cannot_be_empty():
    with pytest.raises(ValidationError):
        Source.model_validate({**MINIMAL, "endpoints": {}})


def test_placeholders():
    assert placeholders("output/{source}/assets/{assetType}_p{page}.json") == [
        "source",
        "assetType",
        "page",
    ]
    assert placeholders("/tenant/info") == []


def test_output_template_falls_back_to_defaults():
    source = load_source(REFERENCE_SOURCE)
    assert source.output_template("tenant_info") == source.defaults.output
    assert source.output_template("measures") == "output/{source}/measures/{id}.json"
