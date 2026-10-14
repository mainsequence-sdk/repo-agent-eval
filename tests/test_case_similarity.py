from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
import pytest
import yaml

from ms_agent_eval.core import case_similarity as similarity
from ms_agent_eval.core.cli import main
from ms_agent_eval.core.errors import ConfigurationError, IntegrityError
from ms_agent_eval.core.evaluation import CaseDefinition
from ms_agent_eval.core.garbage_collection import collect_artifact_garbage
from ms_agent_eval.core.storage import FilesystemArtifactStore

from .helpers import create_case, create_workspace


def _rehash(case: CaseDefinition) -> CaseDefinition:
    path = case.path / "case.yaml"
    metadata = yaml.safe_load(path.read_text())
    metadata["provenance"]["case_content_hash"] = "pending"
    path.write_text(yaml.safe_dump(metadata))
    metadata["provenance"]["case_content_hash"] = CaseDefinition.load(
        case.path, verify_provenance=False
    ).content_hash
    path.write_text(yaml.safe_dump(metadata))
    return CaseDefinition.load(case.path)


def _case(repository, case_id, text, *, skill="alpha", expected="reference answer"):
    case = create_case(
        repository.cases_root,
        case_id=case_id,
        skill=skill,
        group=f"group-{case_id}",
        source_path=f"skills/{skill}/SKILL.md",
    )
    case.solver_prompt_path.write_text(text)
    case.judge_expected_path.write_text(expected)
    return _rehash(case)


def _splits(repository, assignments):
    repository.split_file.write_text(yaml.safe_dump({"schema_version": 3, "groups": assignments}))


def _summary(result):
    return json.loads(Path(result["reports"][0]["summary"]).read_text())


def test_tfidf_known_cosines_unicode_and_empty_vectors():
    matrix, vocabulary = similarity.fit_tfidf(["CAFÉ asset", "café ASSET", "other value", "a !"])
    assert vocabulary == ["asset", "café", "other", "value"]
    np.testing.assert_allclose(
        (matrix @ matrix.T).toarray(), [[1, 1, 0, 0], [1, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 0]]
    )
    result, pairs = similarity.compare_vectors(
        matrix,
        ["g0", "g0", "g1", "g2"],
        ["train", "train", "test", "test"],
        ["a", "a", "b", "c"],
        top_k=1,
        block_size=2,
    )
    assert result["empty_vector_indices"] == [3]
    assert result["categories"]["all"]["pairs"] == 6
    assert result["categories"]["all"]["scorable_pairs"] == 3
    assert result["categories"]["all"]["exact_pairs"] == 1
    assert all(3 not in pair for pair in pairs)


def test_blocked_comparison_and_ties_match_dense_reference():
    texts = ["alpha beta", "alpha beta", "alpha gamma", "delta", "a", "alpha beta"]
    matrix, _ = similarity.fit_tfidf(texts)
    groups = ["a", "a", "b", "c", "d", "e"]
    splits = ["train", "train", "test", "challenge", "test", "test"]
    results = [
        similarity.compare_vectors(matrix, groups, splits, texts, top_k=2, block_size=size)
        for size in (1, 3, 20)
    ]
    assert results[0] == results[1] == results[2]
    summary, pairs = results[0]
    dense = (matrix @ matrix.T).toarray()
    expected = sorted(
        ((dense[i, j], i, j) for i in range(6) for j in range(i + 1, 6) if i != 4 and j != 4),
        key=lambda row: (-row[0], row[1], row[2]),
    )
    observed = summary["categories"]["all"]["top_pairs"]
    assert [(row["left"], row["right"]) for row in observed] == [(i, j) for _, i, j in expected]
    assert summary["categories"]["cross_split"]["exact_pairs"] == 2
    assert all(i < j for i, j in pairs)
    assert sum(item["pairs"] for item in summary["categories"]["all"]["histogram"]) == 10


def test_views_keep_metadata_and_filenames_out_of_vectors(tmp_path):
    repo = create_workspace(tmp_path / "workspace")
    a = _case(repo, "one", "Prompt text", expected="Expected text")
    b = _case(repo, "two", "Prompt text", expected="Other answer")
    assert similarity.render_view(a, "solver") == similarity.render_view(b, "solver")
    assert similarity.render_view(a, "expected") != similarity.render_view(b, "expected")
    for case, filename in ((a, "one.txt"), (b, "different.txt")):
        attachments = case.path / "solver" / "attachments"
        attachments.mkdir()
        (attachments / filename).write_text("shared fixture")
        metadata_path = case.path / "case.yaml"
        metadata = yaml.safe_load(metadata_path.read_text())
        metadata["solver"]["attachments"] = "solver/attachments"
        metadata_path.write_text(yaml.safe_dump(metadata))
    a, b = _rehash(a), _rehash(b)
    text_a, hash_a = similarity.render_view(a, "solver")
    text_b, hash_b = similarity.render_view(b, "solver")
    assert text_a == text_b == "Prompt text\n\nshared fixture"
    assert hash_a != hash_b


def test_cli_audits_without_compile_or_credentials_and_keeps_artifacts_external(
    tmp_path, monkeypatch, capsys
):
    repo = create_workspace(tmp_path / "workspace")
    _case(repo, "one", "shared exact input")
    _case(repo, "two", "shared exact input", expected="a distinct reference")
    _case(repo, "three", "unrelated words")
    _case(repo, "other-skill", "shared exact input", skill="beta")
    _splits(
        repo,
        {
            "group-one": "train",
            "group-two": "test",
            "group-three": "challenge",
            "group-other-skill": "train",
        },
    )
    before = {path: path.read_bytes() for path in repo.root.rglob("*") if path.is_file()}

    def forbidden(*args, **kwargs):
        raise AssertionError("audit must not compile or resolve credentials")

    from ms_agent_eval.core import planning

    monkeypatch.setattr(planning, "compile_workspace", forbidden)
    monkeypatch.setattr(type(repo), "environment", forbidden)
    assert (
        main(
            [
                "cases",
                "similarity",
                "--workspace",
                str(repo.workspace_file),
                "--skill",
                "alpha",
                "--view",
                "both",
                "--export-pairs",
                "--json",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["case_count"] == 3 and result["pair_count"] == 3
    summary = _summary(result)
    assert summary["views"]["solver"]["categories"]["cross_split"]["exact_pairs"] == 1
    assert summary["views"]["expected"]["categories"]["cross_split"]["exact_pairs"] == 1
    path = Path(result["reports"][0]["summary"]).parent
    with (path / "pairs.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 3 and len({(r["case_a"], r["case_b"]) for r in rows}) == 3
    assert all(r["case_a"] < r["case_b"] for r in rows)
    assert before == {p: p.read_bytes() for p in repo.root.rglob("*") if p.is_file()}
    store = FilesystemArtifactStore(repo.data_root, workspace_root=repo.root)
    gc = collect_artifact_garbage(store, minimum_age_seconds=0)
    assert gc["blobs"]["unreferenced_count"] == 0


def test_missing_splits_empty_skill_and_invalid_inputs(tmp_path):
    repo = create_workspace(tmp_path / "workspace")
    _case(repo, "one", "something here")
    single = _summary(similarity.audit_case_similarity(repo, skills=["alpha"]))
    assert single["status"] == "insufficient_cases" and single["pair_count"] == 0
    assert single["views"]["solver"]["categories"]["cross_split"] is None
    empty = _summary(similarity.audit_case_similarity(repo, skills=["gamma"]))
    assert empty["case_count"] == 0 and empty["pair_count"] == 0
    with pytest.raises(ConfigurationError, match="unknown"):
        similarity.audit_case_similarity(repo, skills=["absent"])
    with pytest.raises(ConfigurationError, match="positive"):
        similarity.audit_case_similarity(repo, top_k=0)
    _splits(repo, {})
    with pytest.raises(IntegrityError, match="split groups differ"):
        similarity.audit_case_similarity(repo, skills=["alpha"])


def test_vector_cache_reuse_and_corpus_invalidation(tmp_path, monkeypatch):
    repo = create_workspace(tmp_path / "workspace")
    a = _case(repo, "one", "shared words")
    _case(repo, "two", "shared words different")
    initial = similarity.audit_case_similarity(repo, skills=["alpha"], top_k=1, block_size=1)
    again = similarity.audit_case_similarity(repo, skills=["alpha"], top_k=1, block_size=20)
    assert initial == again
    original_fit = similarity.fit_tfidf

    def forbidden(*args, **kwargs):
        raise AssertionError("vector fitting should be cached")

    monkeypatch.setattr(similarity, "fit_tfidf", forbidden)
    updated_top_k = similarity.audit_case_similarity(repo, skills=["alpha"], top_k=2)
    assert initial["reports"][0]["audit_id"] == updated_top_k["reports"][0]["audit_id"]
    metadata_path = a.path / "case.yaml"
    metadata = yaml.safe_load(metadata_path.read_text())
    metadata["title"] = "New classification title"
    metadata_path.write_text(yaml.safe_dump(metadata))
    _rehash(a)
    updated_metadata = similarity.audit_case_similarity(repo, skills=["alpha"], top_k=2)
    assert updated_metadata["reports"][0]["audit_id"] != updated_top_k["reports"][0]["audit_id"]
    _case(repo, "three", "shared new words")
    with pytest.raises(AssertionError, match="cached"):
        similarity.audit_case_similarity(repo, skills=["alpha"])
    monkeypatch.setattr(similarity, "fit_tfidf", original_fit)
    changed = similarity.audit_case_similarity(repo, skills=["alpha"])
    assert changed["pair_count"] == 3
    assert (
        len(list((repo.data_root / "operations/manifests/case-similarity-vectors").glob("*.json")))
        == 2
    )


def test_tampered_case_and_nonfinite_vectors_are_rejected(tmp_path):
    repo = create_workspace(tmp_path / "workspace")
    case = _case(repo, "one", "original words")
    case.solver_prompt_path.write_text("modified words")
    with pytest.raises(IntegrityError, match="changed after authoring"):
        similarity.audit_case_similarity(repo, skills=["alpha"])
    matrix, _ = similarity.fit_tfidf(["some words"])
    matrix.data[0] = np.nan
    with pytest.raises(IntegrityError, match="finite"):
        similarity.compare_vectors(matrix, ["a"], ["train"], ["hash"], top_k=1, block_size=1)
