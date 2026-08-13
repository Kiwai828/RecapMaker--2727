import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

js = types.ModuleType("js")
js.Object = types.SimpleNamespace(fromEntries=lambda value: value)
js.fetch = None
sys.modules["js"] = js
pyodide = types.ModuleType("pyodide")
pyodide_ffi = types.ModuleType("pyodide.ffi")
pyodide_ffi.to_js = lambda value, **kwargs: value
sys.modules["pyodide"] = pyodide
sys.modules["pyodide.ffi"] = pyodide_ffi

import ai_providers


def test_catalog_item_and_stt_segment_fallback():
    item = ai_providers._catalog_item("openrouter_stt", {"id": "openai/whisper-large-v3", "name": "Whisper", "pricing": {"prompt": "0.0015", "completion": "0"}, "architecture": {"input_modalities": ["audio"], "output_modalities": ["transcription"]}})
    assert item["model_id"] == "openai/whisper-large-v3"
    assert item["is_free"] is False
    segments = ai_providers._stt_segments({"text": "hello", "duration": 2.5})
    assert segments == [{"id": "seg-1", "start_ms": 0, "end_ms": 2500, "original_text": "hello"}]


def test_translation_json_parser_removes_fences():
    parsed = ai_providers._json_from_content('```json\n{"segments": []}\n```')
    assert parsed == {"segments": []}
