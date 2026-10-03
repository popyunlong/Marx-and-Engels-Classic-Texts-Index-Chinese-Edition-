import pytest
from scripts.catalog_browser_review import record


def test_checkpoints_resume_without_losing_prior_checks(tmp_path):
    path = tmp_path / 'browser.json'
    record(path, 'release1', {'id': 'catalog1'}, 'link1', {'result': 'pass', 'target': 's26'})
    report = record(path, 'release1', {'id': 'catalog1'}, 'link2', {'result': 'fail', 'target': 's422'})
    assert len(report['checks']) == 2 and report['checks']['link1']['target'] == 's26'
    report = record(path, 'release1', {'id': 'catalog1'}, 'link2', {'result': 'pass', 'target': 's422'})
    assert len(report['checks']) == 2 and report['checks']['link2']['result'] == 'pass'
    with pytest.raises(ValueError, match='another candidate'):
        record(path, 'release2', {'id': 'catalog1'}, 'link3', {'result': 'pass'})
