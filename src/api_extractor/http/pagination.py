"""The pagination walk.

One file per page, always. Pages are never merged into one array — merging is a downstream
concern and it destroys the record of which page a row came from.

The only style that ships is `page_number`: a cursor that starts at `start` and increments.
What varies between APIs is *where* it goes, which `at` says — a query param, or any depth
inside a JSON body.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from jsonpath_ng.ext import parse as parse_jsonpath

from api_extractor.config.models import Paginate
from api_extractor.http.client import Request


def set_nested(container: dict[str, Any], keys: Sequence[str], value: Any) -> None:
    """Put `value` at `keys`, building the intermediate mappings the API expects."""
    node = container
    for key in keys[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[keys[-1]] = value


def with_cursor(request: Request, paginate: Paginate, cursor: int) -> Request:
    """A copy of the request carrying the page cursor at `at`."""
    if paginate.at_root == "query":
        query = dict(request.query)
        set_nested(query, paginate.at_keys, cursor)
        return replace(request, query=query)
    payload = copy.deepcopy(request.payload)
    if not isinstance(payload, dict):
        payload = {}
    set_nested(payload, paginate.at_keys, cursor)
    return replace(request, payload=payload)


def is_empty_page(body: Any) -> bool:
    """Nothing came back.

    A falsy body is empty. So is a mapping whose list-valued keys are all empty, which is
    how `{"data": [], "total": 0}` announces the end without a flag.
    """
    if not body:
        return True
    if isinstance(body, dict):
        lists = [value for value in body.values() if isinstance(value, list)]
        return bool(lists) and all(not value for value in lists)
    return False


def has_next(paginate: Paginate, body: Any) -> bool:
    """Whether to ask for another page. `max_pages` still caps whatever this says."""
    if is_empty_page(body):
        return False
    if paginate.has_more is None:
        return True
    matches = [match.value for match in parse_jsonpath(paginate.has_more).find(body)]
    return bool(matches) and bool(matches[0])
