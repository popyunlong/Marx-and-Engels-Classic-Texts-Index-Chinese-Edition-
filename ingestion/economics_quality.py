"""Fail-closed release hold for the isolated economics28 batch."""
from pathlib import Path
import json


def require_release_clear(root):
    if (Path(root) / 'quality-hold.json').exists():
        raise ValueError('economics28 quality hold: original-image revalidation required')


def require_current_bibliography(metadata, rows):
    """Withdrawn decisions cannot attest retained bibliography fields."""
    evidence = []
    for row in rows:
        if not row['reviewed'] or row['kind'] not in ('cover', 'copyright', 'other'):
            continue
        decision = json.loads(row['decision'])
        if (decision.get('action') == 'confirm'
                and decision.get('complete_text_review') is True
                and decision.get('image_viewed') is True
                and decision.get('image_sha256') == row['image_hash']):
            evidence.append(decision.get('metadata', {}))
    missing = [field for field, value in metadata.items()
               if not any(field in proof and proof[field] == value for proof in evidence)]
    if missing:
        raise ValueError('bibliography lacks current image-reviewed evidence: ' + ', '.join(sorted(missing)))
