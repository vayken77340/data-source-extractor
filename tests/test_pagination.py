from __future__ import annotations

import pytest

from api_extractor.config.models import Paginate
from api_extractor.http.client import Request
from api_extractor.http.pagination import has_next, is_empty_page, set_nested, with_cursor

BASE = Request(method="POST", url="https://demo.example.com/things")


def paginate(**overrides) -> Paginate:
    return Paginate.model_validate({"style": "page_number", "at": "query.page", **overrides})


# --- putting the cursor where `at` says ---------------------------------------------


def test_cursor_into_a_query_param():
    request = with_cursor(BASE, paginate(at="query.page"), 3)
    assert request.query == {"page": 3}


def test_cursor_alongside_existing_query_params():
    request = with_cursor(
        Request(method="GET", url=BASE.url, query={"per_page": 100}), paginate(at="query.page"), 2
    )
    assert request.query == {"per_page": 100, "page": 2}


def test_cursor_nested_in_the_payload():
    """{"pageLink": {"page": 0, "pageSize": 100}} — the shape a real API wanted."""
    request = Request(
        method="POST",
        url=BASE.url,
        payload={"pageLink": {"pageSize": 100, "sortOrder": {"property": "name"}}},
    )
    updated = with_cursor(request, paginate(at="payload.pageLink.page"), 4)
    assert updated.payload == {
        "pageLink": {"pageSize": 100, "sortOrder": {"property": "name"}, "page": 4}
    }


def test_the_original_request_is_untouched():
    """Every page starts from the planned payload, not the last page's."""
    request = Request(method="POST", url=BASE.url, payload={"pageLink": {"pageSize": 100}})
    with_cursor(request, paginate(at="payload.pageLink.page"), 1)
    assert request.payload == {"pageLink": {"pageSize": 100}}


def test_missing_intermediate_mappings_are_created():
    request = with_cursor(BASE, paginate(at="payload.a.b.c"), 7)
    assert request.payload == {"a": {"b": {"c": 7}}}


def test_an_existing_cursor_value_is_overwritten():
    request = Request(method="POST", url=BASE.url, payload={"pageLink": {"page": 0}})
    assert with_cursor(request, paginate(at="payload.pageLink.page"), 9).payload == {
        "pageLink": {"page": 9}
    }


def test_set_nested_directly():
    container: dict = {}
    set_nested(container, ["a", "b"], 1)
    assert container == {"a": {"b": 1}}
    set_nested(container, ["a", "c"], 2)
    assert container == {"a": {"b": 1, "c": 2}}


# --- when to stop --------------------------------------------------------------------


@pytest.mark.parametrize("body", [None, {}, [], "", {"data": []}, {"data": [], "total": 0}])
def test_empty_pages(body):
    assert is_empty_page(body) is True


@pytest.mark.parametrize("body", [{"data": [1]}, [1], {"total": 3}, {"data": [], "other": [1]}])
def test_non_empty_pages(body):
    assert is_empty_page(body) is False


def test_has_more_true_keeps_going():
    assert has_next(paginate(has_more="$.hasNext"), {"data": [1], "hasNext": True}) is True


def test_has_more_false_stops():
    assert has_next(paginate(has_more="$.hasNext"), {"data": [1], "hasNext": False}) is False


def test_a_missing_has_more_path_stops():
    """Absent is not 'keep going' — an unreadable stop condition stops."""
    assert has_next(paginate(has_more="$.hasNext"), {"data": [1]}) is False


def test_an_empty_page_stops_even_when_has_more_says_otherwise():
    assert has_next(paginate(has_more="$.hasNext"), {"data": [], "hasNext": True}) is False


def test_without_has_more_the_walk_runs_until_a_page_is_empty():
    condition = paginate()
    assert condition.has_more is None
    assert has_next(condition, {"data": [1]}) is True
    assert has_next(condition, {"data": []}) is False
