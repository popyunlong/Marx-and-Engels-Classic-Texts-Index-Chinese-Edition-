"""Use the installed offline OCR runtime for one bounded recommendation page."""
import json
import contextlib
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ingestion.free_ocr import engine, recognize, render, validate

if __name__ == '__main__':
    with contextlib.redirect_stdout(sys.stderr):
        result = recognize(engine(os.environ.get('RECOMMENDATION_OCR_MODELS', '/opt/marx-ingestion-ocr/models'), threads=1),
                           render(sys.argv[1], int(sys.argv[2])))
    validate(result)
    print(json.dumps({'text': result['text']}, ensure_ascii=False))
