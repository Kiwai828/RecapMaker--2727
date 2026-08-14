import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

# The parser tests do not call Workers APIs; provide local import shims for the
# Cloudflare JavaScript FFI modules used by ai_providers.py.
js = types.ModuleType("js")
js.Object = types.SimpleNamespace(fromEntries=lambda value: value)
js.fetch = None
sys.modules.setdefault("js", js)
pyodide = types.ModuleType("pyodide")
ffi = types.ModuleType("pyodide.ffi")
ffi.to_js = lambda value, **kwargs: value
pyodide.ffi = ffi
sys.modules.setdefault("pyodide", pyodide)
sys.modules.setdefault("pyodide.ffi", ffi)

from ai_providers import AIProviderResponseError, _json_from_content, _output_tokens_for_segments, target_language_label, translation_chunks


def test_translation_json_parser_accepts_preamble_and_fences():
    content = "Here is the JSON:\n```json\n{\"segments\":[{\"id\":\"seg-1\",\"translated_text\":\"မင်္ဂလာပါ\",\"tts_text\":\"မင်္ဂလာပါ\"}]}\n```"
    parsed = _json_from_content(content, "gemini")
    assert parsed["segments"][0]["id"] == "seg-1"


def test_translation_json_parser_rejects_truncated_string_with_provider_error():
    content = '{"segments":[{"id":"seg-1","translated_text":"unterminated'
    try:
        _json_from_content(content, "opencode_zen")
    except AIProviderResponseError as exc:
        assert exc.code == "AI_PROVIDER_MALFORMED_RESPONSE"
        assert exc.provider == "opencode_zen"
    else:
        raise AssertionError("truncated JSON must raise AIProviderResponseError")


def test_translation_chunks_preserve_order_and_do_not_split_segments():
    segments = [{"id": f"seg-{i}", "original_text": "x" * 100} for i in range(30)]
    chunks = translation_chunks(segments, max_chars=700)
    assert len(chunks) > 1
    assert [item["id"] for chunk in chunks for item in chunk] == [item["id"] for item in segments]
    assert all(chunk for chunk in chunks)


def test_output_budget_has_no_artificial_32768_ceiling():
    assert _output_tokens_for_segments(100, 640) == 64000


def test_burmese_target_language_uses_explicit_locale_label():
    assert target_language_label("my") == "Burmese (Myanmar)"
    assert target_language_label("my-MM") == "Burmese (Myanmar)"
