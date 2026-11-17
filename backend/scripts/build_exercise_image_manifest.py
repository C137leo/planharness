"""Rebuild exact-action mappings from existing generated project images."""
import json
import ast
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
assets = root / 'frontend/src/static/ex'
mapping = {p.stem: f'/static/ex/generated-v2/{p.name}' for p in sorted((assets / 'generated-v2').glob('*')) if p.suffix in ('.png', '.jpg')}
for slug in ('wall-sit', 'back-extension'):
    for extension in ('png', 'jpg'):
        path = assets / f'{slug}-v2.{extension}'
        if path.is_file():
            mapping[slug] = f'/static/ex/{path.name}'
(root / 'frontend/src/styles/exerciseImages.json').write_text(json.dumps(mapping, ensure_ascii=False, indent=2) + '\n')
print(f'Exact action images mapped: {len(mapping)}')
if '--check-complete' in sys.argv:
    tree = ast.parse((root / 'backend/app/domain/exercise_catalog.py').read_text())
    catalog = ast.literal_eval(next(node.value for node in tree.body if isinstance(node, ast.AnnAssign)))
    missing = sorted(set(catalog) - set(mapping))
    if missing:
        raise SystemExit(f'{len(missing)} action images missing: {missing}')
    print(f'Full catalog verified: {len(catalog)} exact action mappings')
