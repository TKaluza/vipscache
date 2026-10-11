"""Keep deprecated public helpers compatible while guiding callers to replacements."""

from dataclasses import replace

import pytest

from vipscache import ImageSpec, MaterializePolicy, NodeSpec, Operation, SourceSpec


def test_deprecated_node_helper_preserves_identity_and_policy():
    node = NodeSpec("a" * 32, Operation("scale", {"width": 32}))
    # Reflect over the deprecated API deliberately; regular callers use replace().
    helper = getattr(node, "with_materialize")
    with pytest.warns(DeprecationWarning, match="dataclasses.replace") as recorded:
        pinned = helper(MaterializePolicy.PIN)
    assert recorded[0].filename == __file__
    assert pinned == replace(
        node, operation=replace(node.operation, materialize=MaterializePolicy.PIN)
    )
    assert pinned.key == node.key
    assert node.materialize == MaterializePolicy.NEVER


@pytest.mark.parametrize("encoded", [False, True])
def test_deprecated_leaf_property_preserves_result(encoded):
    spec = ImageSpec(SourceSpec("a" * 32))
    if encoded:
        spec = spec.with_encode(Operation("encode", {"format": "png"}))
    with pytest.warns(DeprecationWarning, match="spec.encode is not None") as recorded:
        result = getattr(spec, "is_materializable_leaf")
    assert recorded[0].filename == __file__
    assert result is encoded


def test_canonical_order_rejects_unimplemented_alpha():
    with pytest.raises(ValueError, match="unsupported canonical operation: alpha"):
        ImageSpec.canonical(
            SourceSpec("a" * 32), [Operation("alpha")], Operation("encode", {"format": "png"})
        )
