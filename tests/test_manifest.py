from keturah import validate_manifest

from mahalath.contract import MATCH_FIELDS
from mahalath.manifest import build_manifest


def test_manifest_conforms_and_matches_the_contract():
    m = build_manifest()
    assert validate_manifest(m) == []
    assert m.product == "mahalath"
    retrieve = next(c for c in m.capabilities if c.name == "retrieve")
    item_props = retrieve.output_schema["properties"]["matches"]["items"]["properties"]
    assert set(item_props) == set(MATCH_FIELDS)  # output schema built from the Match contract
    assert "retrieve" in [t["name"] for t in m.to_mcp()["tools"]]
