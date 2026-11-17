from app.knowledge.service import _knowledge_items
from app.knowledge.provenance import check_offline


def test_guidance_sources_and_unique_ids():
    items = _knowledge_items()
    assert len({item['id'] for item in items}) == len(items)
    guidance = [item for item in items if item.get('action_ref')]
    assert check_offline(guidance)[0] == []


def test_missing_exercise_guidance_matches(client, auth_headers):
    for name, expected in [('靠墙静蹲', 'wall-sit-cue'), ('山羊挺身', 'back-extension-cue')]:
        response = client.post('/api/v1/knowledge/ask', headers=auth_headers,
                               json={'question': name + '的动作要领'})
        assert response.status_code == 200
        data = response.json()['data']
        assert not data['refused']
        assert data['citations'][0]['id'] == expected
        assert name in data['answer']


def test_catalog_audit_and_guided_selection():
    from types import SimpleNamespace
    from app.domain.guided_selection import GUIDANCE_CATALOG, has_guidance, prefer_guided_actions
    from app.domain.exercise_catalog import EXERCISE_CATALOG
    assert set(GUIDANCE_CATALOG) == set(EXERCISE_CATALOG)
    assert has_guidance('wall-sit') and has_guidance('back-extension')
    assert not has_guidance('banded-dead-bug')
    for item in GUIDANCE_CATALOG.values():
        if item.get('upstream_id'):
            assert item['instructions'] and item['source_url'].endswith(item['upstream_id'] + '.json')
    def payload():
        return {'status': 'ready', 'days': [{'actions': [{'action_ref':'hip-thrust', 'slot':'strength',
          'sets':3, 'meta':{'movement_mode':'hip_dominant'}}]}]}
    p = payload()
    prefer_guided_actions(p, SimpleNamespace(equipment=['yoga_mat']))
    assert p['days'][0]['actions'][0]['action_ref'] == 'glute-bridge'
    p = payload()
    prefer_guided_actions(p, SimpleNamespace(equipment=['yoga_mat']), {'dislikes':['臀桥']})
    assert p['days'][0]['actions'][0]['action_ref'] == 'hip-thrust'


def test_guidance_never_overrides_injury_filter(monkeypatch):
    from types import SimpleNamespace
    from app.domain.guided_selection import prefer_guided_actions
    from app.domain import safety_rules
    seen = []
    def risky(ref, region, meta=None):
        seen.append((ref, region))
        return True
    monkeypatch.setattr(safety_rules, 'is_risky_for', risky)
    payload = {'status': 'ready', 'days': [{'actions': [{'action_ref':'hip-thrust', 'slot':'strength',
        'meta':{'movement_mode':'hip_dominant'}}]}]}
    prefer_guided_actions(payload, SimpleNamespace(equipment=['mat']), {'injuries_long_term':['腰痛']})
    assert seen
    assert payload['days'][0]['actions'][0]['action_ref'] == 'hip-thrust'
