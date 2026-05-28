from imgcache.spec import DerivativeSpec, EncodeSpec, MaterializePolicy, NodeSpec, Operation, SourceSpec


def test_operation_params_are_canonicalized_for_keys():
    first = NodeSpec("parent", Operation("crop", {"x": 1, "y": 2, "w": 3, "h": 4}))
    second = NodeSpec("parent", Operation("crop", {"h": 4, "w": 3, "y": 2, "x": 1}))

    assert first.key == second.key


def test_operation_order_is_part_of_derivative_key():
    source = SourceSpec("file", "/tmp/source.png")
    crop = Operation("crop", {"x": 0, "y": 0, "w": 10, "h": 10})
    rotate = Operation("fast_rotate", {"degrees": 90})

    crop_then_rotate = DerivativeSpec.build(
        source,
        [crop, rotate],
        Operation("encode", {"format": "png"}),
    )
    rotate_then_crop = DerivativeSpec.build(
        source,
        [rotate, crop],
        Operation("encode", {"format": "png"}),
    )

    assert crop_then_rotate.leaf.key != rotate_then_crop.leaf.key


def test_encode_spec_excludes_materialization_policy_from_key():
    forced = NodeSpec("parent", Operation("render", {"width": 100}, MaterializePolicy.FORCE))
    pinned = NodeSpec("parent", Operation("render", {"width": 100}, MaterializePolicy.PIN))
    leaf = EncodeSpec(forced.key, "webp", {"quality": 82})

    assert forced.key == pinned.key
    assert leaf.extension == "webp"


def test_operation_defaults_to_no_intermediate_materialization():
    assert Operation("crop").materialize == MaterializePolicy.NEVER


def test_canonical_builder_enforces_operation_order_for_pdf():
    source = SourceSpec("file", "/tmp/source.pdf", mime="application/pdf")
    spec = DerivativeSpec.canonical(
        source,
        [
            Operation("crop", {"x": 0, "y": 0, "w": 10, "h": 10}),
            Operation("rotate", {"degrees": 12}),
            Operation("render", {"page": 1, "dpi": 75}),
            Operation("scale", {"width": 100}),
            Operation("fast_rotate", {"degrees": 90}),
            Operation("normalize", {"colorspace": "srgb"}),
        ],
        Operation("encode", {"format": "png"}),
    )

    assert [node.operation.name for node in spec.nodes] == [
        "render",
        "normalize",
        "fast_rotate",
        "crop",
        "scale",
        "rotate",
    ]


def test_canonical_builder_skips_render_for_image_sources():
    source = SourceSpec("file", "/tmp/source.jpg", mime="image/jpeg")
    spec = DerivativeSpec.canonical(
        source,
        [
            Operation("render", {"page": 1, "dpi": 75}, MaterializePolicy.FORCE),
            Operation("colorspace", {"colorspace": "srgb"}),
            Operation("crop", {"x": 0, "y": 0, "w": 10, "h": 10}),
        ],
        Operation("encode", {"format": "webp"}),
    )

    assert [node.operation.name for node in spec.nodes] == ["colorspace", "crop"]


def test_derivative_spec_payload_round_trips():
    source = SourceSpec("file", "/tmp/source.pdf", mime="application/pdf")
    spec = DerivativeSpec.canonical(
        source,
        [
            Operation("render", {"page": 1, "dpi": 75}, MaterializePolicy.FORCE),
            Operation("crop", {"x": 0, "y": 0, "w": 10, "h": 10}),
        ],
        Operation("encode", {"format": "png"}),
    )

    restored = DerivativeSpec.from_payload(spec.to_payload())

    assert restored == spec
