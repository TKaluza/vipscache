import json
from pathlib import Path

from vipscache.hash import canonical_json, file_id, xxh3_128_hexdigest
from vipscache.layout import CacheLayout
from vipscache.spec import ImageSpec

VECTORS = json.loads((Path(__file__).parents[1] / 'contracts/vectors.json').read_text())


def test_shared_file_hashes(tmp_path):
    for case in VECTORS['files']:
        path = tmp_path / case['name']
        path.write_bytes(bytes.fromhex(case['hex']) * case['repeat'])
        assert file_id(path) == case['hash']


def test_shared_canonical_json():
    for case in VECTORS['canonical']:
        data = canonical_json(case['input'])
        assert data.decode() == case['json']
        assert xxh3_128_hexdigest(data) == case['hash']


def test_shared_specs():
    for case in VECTORS['specs']:
        spec = ImageSpec.from_payload(case['input'])
        assert spec.to_payload() == case['payload']
        key = spec.leaf.key if spec.encode else spec.source.file_id
        path = 'cache/' + str(CacheLayout(Path('.')).leaf_relpath(key, spec.leaf.extension)) if spec.encode else 'raw/' + key
        assert key == case['key']
        assert path == case['relpath']


def test_integral_floats_share_integer_keys():
    assert canonical_json({'n': [1.0, -0.0, {'a': 42.0}]}) == canonical_json({'n': [1, 0, {'a': 42}]})
    for value in [float('nan'), float('inf'), float('-inf')]:
        import pytest
        with pytest.raises(ValueError):
            canonical_json(value)
