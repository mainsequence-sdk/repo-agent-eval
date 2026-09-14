from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

from ms_agent_eval.core.cli import main
from ms_agent_eval.core.model_catalog import (
    load_openrouter_catalog,
    match_model,
    rank_model_matches,
    refresh_openrouter_catalog,
    upsert_openrouter_models,
)


class _Response(BytesIO):
    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *_: object) -> None:
        return None


def _remote_model(
    model: str = "qwen/qwen3.6-27b",
    *,
    prompt: str = "0.0000003",
) -> dict[str, object]:
    return {
        "id": model,
        "canonical_slug": f"{model}-20260422",
        "hugging_face_id": model,
        "name": model,
        "context_length": 262144,
        "architecture": {
            "modality": "text+image->text",
            "input_modalities": ["text", "image"],
            "output_modalities": ["text"],
        },
        "supported_parameters": ["tools", "reasoning"],
        "reasoning": {"mandatory": False},
        "pricing": {
            "prompt": prompt,
            "completion": "0.000002",
            "input_cache_read": "0.00000003",
            "input_cache_write": "0.000000375",
            "web_search": "0.01",
            "overrides": [{"min_prompt_tokens": 128000, "prompt": "0.0000006"}],
        },
    }


def test_upsert_preserves_custom_and_absent_existing_models(tmp_path: Path) -> None:
    catalog_path = tmp_path / "models" / "openrouter.json"
    catalog_path.parent.mkdir()
    catalog_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "models": [
                    {
                        "model": "custom/local-agent",
                        "source": "custom",
                        "provider": "local",
                        "pricing": {},
                    },
                    {
                        "model": "retired/keep-me",
                        "source": "custom",
                        "provider": "retired",
                        "pricing": {},
                    },
                    {"model": "qwen/qwen3.6-27b", "input_price_per_million": 99.0},
                ],
            }
        ),
        encoding="utf-8",
    )

    catalog = upsert_openrouter_models(catalog_path, [_remote_model()])

    assert set(catalog.by_model()) == {
        "custom/local-agent",
        "qwen/qwen3.6-27b",
        "retired/keep-me",
    }
    qwen = catalog.by_model()["qwen/qwen3.6-27b"]
    assert qwen["source"] == "openrouter"
    assert qwen["provider"] == "qwen"
    assert qwen["context_length"] == 262144
    assert qwen["input_price_per_million"] == 0.3
    assert qwen["output_price_per_million"] == 2.0
    assert qwen["cache_read_price_per_million"] == 0.03
    assert qwen["cache_write_price_per_million"] == 0.375
    assert qwen["pricing"] == _remote_model()["pricing"]
    assert "updated_at" not in json.loads(catalog_path.read_text(encoding="utf-8"))


def test_refresh_fetches_the_openrouter_endpoint_and_upserts(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    captured: dict[str, object] = {}

    def fake_urlopen(request, *, timeout):  # type: ignore[no-untyped-def]
        captured["url"] = request.full_url
        captured["accept"] = request.headers["Accept"]
        captured["timeout"] = timeout
        return _Response(json.dumps({"data": [_remote_model()]}).encode())

    monkeypatch.setattr("ms_agent_eval.core.openrouter_catalog.urlopen", fake_urlopen)
    path = tmp_path / "openrouter.json"

    catalog = refresh_openrouter_catalog(path)

    assert tuple(catalog.by_model()) == ("qwen/qwen3.6-27b",)
    assert captured == {
        "url": "https://openrouter.ai/api/v1/models",
        "accept": "application/json",
        "timeout": 30.0,
    }


def test_model_aware_fuzzy_match_ignores_local_variant_suffixes(tmp_path: Path) -> None:
    path = tmp_path / "openrouter.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "models": [
                    {
                        "model": "qwen3.6:27b-coding",
                        "source": "custom",
                        "provider": "local",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    catalog = upsert_openrouter_models(
        path,
        [
            _remote_model(),
            _remote_model("qwen/qwen3.6-35b-a3b"),
            _remote_model("openai/gpt-5.6-luna"),
        ],
    )

    match = match_model("qwen3.6:27b-coding", catalog)

    assert match is not None
    assert match.model == "qwen/qwen3.6-27b"
    assert match.score > 0.8
    assert [item.model for item in rank_model_matches("qwen3.6:27b-coding", catalog)] == [
        "qwen/qwen3.6-27b",
        "qwen/qwen3.6-35b-a3b",
        "openai/gpt-5.6-luna",
    ]
    assert (
        rank_model_matches("qwen3.6:27b-coding", catalog, limit=1, source=None)[0].model
        == "qwen3.6:27b-coding"
    )


def test_load_missing_catalog_is_empty(tmp_path: Path) -> None:
    catalog = load_openrouter_catalog(tmp_path / "missing.json")

    assert catalog.models == ()


def test_models_refresh_cli_does_not_require_a_workspace(
    monkeypatch, tmp_path: Path, capsys
) -> None:  # type: ignore[no-untyped-def]
    def fake_urlopen(request, *, timeout):  # type: ignore[no-untyped-def]
        return _Response(json.dumps({"data": [_remote_model()]}).encode())

    monkeypatch.setattr("ms_agent_eval.core.openrouter_catalog.urlopen", fake_urlopen)
    path = tmp_path / "models.json"

    exit_code = main(["models", "refresh", "--catalog", str(path)])

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert result == {
        "catalog": path.as_posix(),
        "custom_model_count": 0,
        "model_count": 1,
        "openrouter_model_count": 1,
        "status": "refreshed",
    }


def test_models_match_cli_returns_ranked_pricing_fields(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "models.json"
    upsert_openrouter_models(
        path,
        [
            _remote_model(),
            _remote_model("qwen/qwen3.6-35b-a3b"),
            _remote_model("openai/gpt-5.6-luna"),
        ],
    )

    exit_code = main(
        [
            "models",
            "match",
            "qwen3.6:27b-coding",
            "--catalog",
            str(path),
            "--limit",
            "2",
        ]
    )

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert result["query"] == "qwen3.6:27b-coding"
    assert [match["model"] for match in result["matches"]] == [
        "qwen/qwen3.6-27b",
        "qwen/qwen3.6-35b-a3b",
    ]
    assert result["matches"][0]["similarity"] > 0.8
    assert result["matches"][0]["input_price_per_million"] == 0.3
    assert result["matches"][0]["output_price_per_million"] == 2.0
