"""§6.3 cannot diverge from `envelope.build()`.

The previous template described a metadata contract the code never wrote. This pins the
only remaining description — the catalogue in `specgen/contract.py` — to what the builder
actually writes, in both directions and by type.
"""

from __future__ import annotations

from specgen import contract, model
from specgen.labels import L


def test_the_catalogue_describes_exactly_what_the_envelope_writes():
    parsed, raw = contract.probe()
    assert contract.paths(parsed) == contract.DECLARED - {"body_raw"}
    assert contract.paths(raw) == contract.DECLARED


def test_body_raw_is_present_exactly_when_the_response_is_not_json():
    parsed, raw = contract.probe()
    assert "body_raw" not in parsed and raw["body_raw"] == "<html>"


def test_every_attribute_has_the_type_it_promises():
    for built in contract.probe():
        for attribute in contract.ATTRIBUTES:
            if attribute.path in contract.paths(built):
                value = contract.value_at(built, attribute.path)
                assert contract.type_matches(attribute, value), (attribute.path, value)


def test_a_key_added_to_the_envelope_shows_up_as_undescribed():
    parsed, _ = contract.probe()
    parsed["metadata"]["batch_id"] = "x"
    parsed["metadata"]["request"]["url"] = "x"
    assert contract.paths(parsed) - contract.DECLARED == {"metadata.batch_id", "metadata.request.url"}


def test_a_declared_leaf_is_not_walked_into():
    parsed, _ = contract.probe()
    parsed["metadata"]["params"] = {"deep": {"deeper": 1}}
    assert "metadata.params" in contract.paths(parsed)
    assert not any(path.startswith("metadata.params.") for path in contract.paths(parsed))


def test_type_matching_is_strict_about_booleans_and_timestamps():
    status = next(a for a in contract.ATTRIBUTES if a.path == "metadata.response.status")
    stamp = next(a for a in contract.ATTRIBUTES if a.path == "metadata.extracted_at")
    assert contract.type_matches(status, 200) and not contract.type_matches(status, True)
    assert contract.type_matches(stamp, "2026-09-04T10:12:03Z")
    assert not contract.type_matches(stamp, "2026-09-04 10:12:03")


def test_the_rows_carry_iceberg_types_and_every_attribute():
    rows = contract.rows(["a", "b"])
    assert [row["attribute"] for row in rows] == [a.path for a in contract.ATTRIBUTES]
    assert {row["type"] for row in rows} <= set(L.section("types.contract").values())
    assert {row["mandatory"] for row in rows} <= set(L.section("enums.mandatory").values())


def test_the_label_file_describes_exactly_the_declared_attributes():
    """A description without an attribute, or an attribute without one, both fail."""
    assert contract.described() == contract.DECLARED


def test_the_document_example_is_the_envelope_the_builder_writes(reference_source, reference_annotation):
    """§6.4 is produced by `envelope.build()`, never typed: same paths as the catalogue."""
    built = model.build(reference_source, reference_annotation)
    assert contract.paths(built.model["landing"]["example"]) == contract.DECLARED - {"body_raw"}
