"""Offline authoring diagnostics, isolated from evaluation and optimizer inputs."""

from __future__ import annotations

import csv
import heapq
import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import scipy
from scipy import sparse

from .errors import ConfigurationError, IntegrityError
from .evaluation import CapabilityCatalog, CaseBank, CaseDefinition, SplitManifest
from .hashing import canonical_json_bytes, content_hash, json_value, sha256_bytes
from .storage import FilesystemArtifactStore
from .workspace import WorkspaceRepository


REPRESENTATION = {
    "name": "tfidf",
    "version": 1,
    "renderer": "main-text-then-sorted-artifact-bodies-v1",
    "tokenizer": "unicode-casefold-word-unigrams-min-length-2",
    "tf": "1 + ln(count)",
    "idf": "1 + ln((N + 1) / (df + 1))",
    "normalization": "l2",
    "dtype": "float64",
    "numpy": np.__version__,
    "scipy": scipy.__version__,
}
LIMITATION = (
    "TF-IDF measures lexical overlap. Shared templates can inflate scores and paraphrases "
    "can score poorly. Findings are advisory, not acceptance decisions or proof of leakage. "
    "Audit artifacts must not enter solver, optimizer, or model-selection inputs."
)
_TOKEN = re.compile(r"(?u)\b\w\w+\b")
_EDGES = np.array([0.0, 0.5, 0.7, 0.8, 0.9, 0.95, 0.99, 1.0])
_CATEGORIES = ("all", "same_group", "different_group", "cross_split")


def render_view(case: CaseDefinition, view: str) -> tuple[str, str]:
    """Return vectorized text and the distinct, exact view identity."""
    if view == "solver":
        main, artifacts = case.prompt, case.solver_attachments()
        exact = case.solver_view_hash
    elif view == "expected":
        main, artifacts = case.expected_response, case.expected_artifacts()
        exact = content_hash({"response": main, "artifacts": dict(artifacts)})
    else:
        raise ConfigurationError(f"unknown similarity view: {view}")
    return "\n\n".join([main, *(artifacts[path] for path in sorted(artifacts))]), exact


def fit_tfidf(texts: Sequence[str]) -> tuple[sparse.csr_matrix, list[str]]:
    """Fit the documented weighting without hidden stopwords or model downloads."""
    documents = [Counter(_TOKEN.findall(text.casefold())) for text in texts]
    frequencies = Counter(token for document in documents for token in document)
    vocabulary = sorted(frequencies)
    columns = {token: index for index, token in enumerate(vocabulary)}
    data, indices, indptr = [], [], [0]
    for document in documents:
        weights = [
            (
                columns[token],
                (1 + math.log(count)) * (1 + math.log((len(texts) + 1) / (frequencies[token] + 1))),
            )
            for token, count in sorted(document.items())
        ]
        norm = math.sqrt(math.fsum(value * value for _, value in weights))
        indices.extend(index for index, _ in weights)
        data.extend(value / norm for _, value in weights)
        indptr.append(len(data))
    matrix = sparse.csr_matrix(
        (np.array(data, dtype=np.float64), np.array(indices), np.array(indptr)),
        shape=(len(texts), len(vocabulary)),
    )
    return matrix, vocabulary


def _vectors(
    store: FilesystemArtifactStore, texts: Sequence[str]
) -> tuple[sparse.csr_matrix, dict]:
    identity = {
        "representation": REPRESENTATION,
        "text_hashes": [sha256_bytes(text.encode("utf-8")) for text in texts],
    }
    key = f"case-similarity-vectors/{content_hash(identity).removeprefix('sha256:')}"
    path = store.manifest_root / f"{key}.json"
    if path.exists():
        if path.is_symlink():
            raise IntegrityError("similarity vector manifest must not be a symlink")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest["identity"] != identity:
            raise IntegrityError("similarity vector cache identity mismatch")
        reference = store.reference(manifest["matrix"]["content_id"], "application/octet-stream")
        with store.get_blob(reference) as handle:
            matrix = sparse.load_npz(handle).tocsr()
    else:
        matrix, vocabulary = fit_tfidf(texts)
        payload = BytesIO()
        sparse.save_npz(payload, matrix)
        payload.seek(0)
        reference = store.put_blob(payload, "application/octet-stream")
        manifest = {"identity": identity, "matrix": json_value(reference), "vocabulary": vocabulary}
        store.put_manifest(key, manifest)
    norms = np.asarray(matrix.multiply(matrix).sum(axis=1)).ravel()
    if (
        matrix.shape != (len(texts), len(manifest["vocabulary"]))
        or not np.isfinite(matrix.data).all()
        or (matrix.data < 0).any()
        or not np.all(np.isclose(norms, 1.0) | (norms == 0))
    ):
        raise IntegrityError("similarity cache contains invalid TF-IDF vectors")
    return matrix, manifest


@dataclass
class PairStatistics:
    pairs: int = 0
    scorable: int = 0
    sums: list[float] = field(default_factory=list)
    histogram: np.ndarray = field(default_factory=lambda: np.zeros(len(_EDGES) - 1, dtype=np.int64))
    exact: int = 0
    best: list[tuple[float, int, int]] = field(default_factory=list)

    def add(self, scores: np.ndarray, mask: np.ndarray, exact: np.ndarray, left: int, right: int):
        self.pairs += int(mask.sum())
        self.exact += int((mask & exact).sum())
        valid = mask & np.isfinite(scores)
        values = scores[valid]
        self.scorable += len(values)
        self.sums.append(float(values.sum()))
        self.histogram += np.histogram(values, bins=_EDGES)[0]
        if not len(values):
            return
        # Retain a bounded global ranking; score ties prefer stable case-ID order.
        positions = np.flatnonzero(valid)
        if len(positions) > 20:
            cutoff = np.partition(values, -20)[-20]
            positions = positions[values >= cutoff]
        values = scores.ravel()[positions]
        order = np.lexsort((positions, -values))[:20]
        for position in positions[order]:
            i, j = np.unravel_index(position, scores.shape)
            item = (float(scores[i, j]), -(left + int(i)), -(right + int(j)))
            if len(self.best) < 20:
                heapq.heappush(self.best, item)
            elif item > self.best[0]:
                heapq.heapreplace(self.best, item)

    def result(self) -> dict:
        return {
            "pairs": self.pairs,
            "scorable_pairs": self.scorable,
            "unavailable_pairs": self.pairs - self.scorable,
            "exact_pairs": self.exact,
            "mean_cosine": round(math.fsum(self.sums) / self.scorable, 12)
            if self.scorable
            else None,
            "histogram": [
                {
                    "lower_inclusive": float(low),
                    "upper": float(high),
                    "upper_inclusive": bool(high == 1.0),
                    "pairs": int(count),
                }
                for low, high, count in zip(_EDGES[:-1], _EDGES[1:], self.histogram, strict=True)
            ],
            "top_pairs": [
                {"left": -i, "right": -j, "score": score}
                for score, i, j in sorted(self.best, reverse=True)
            ],
        }


def _merge_neighbors(
    best_scores: np.ndarray,
    best_ids: np.ndarray,
    start: int,
    scores: np.ndarray,
    candidate_ids: np.ndarray,
) -> None:
    end = start + len(scores)
    combined_scores = np.concatenate((best_scores[start:end], scores), axis=1)
    combined_ids = np.concatenate(
        (best_ids[start:end], np.broadcast_to(candidate_ids, scores.shape)), axis=1
    )
    order = np.lexsort((combined_ids, -combined_scores), axis=1)[:, : best_scores.shape[1]]
    best_scores[start:end] = np.take_along_axis(combined_scores, order, axis=1)
    best_ids[start:end] = np.take_along_axis(combined_ids, order, axis=1)


def compare_vectors(
    matrix: sparse.csr_matrix,
    groups: Sequence[str],
    splits: Sequence[str | None],
    hashes: Sequence[str],
    *,
    top_k: int,
    block_size: int,
) -> tuple[dict, set[tuple[int, int]]]:
    """Visit upper-triangle blocks, maintaining exact aggregates and bounded neighbors."""
    n = matrix.shape[0]
    if matrix.shape[0] != len(groups) or len(splits) != n or len(hashes) != n:
        raise IntegrityError("similarity vector and metadata dimensions differ")
    if top_k < 1 or block_size < 1:
        raise ConfigurationError("top-k and block-size must be positive")
    norms = np.asarray(matrix.multiply(matrix).sum(axis=1)).ravel()
    if not np.isfinite(matrix.data).all() or not np.all(np.isclose(norms, 1) | (norms == 0)):
        raise IntegrityError("cosine comparison requires finite, normalized vectors")
    valid_rows = norms > 0
    group_array, split_array, hash_array = map(np.asarray, (groups, splits, hashes))
    has_splits = all(split is not None for split in splits)
    statistics = {category: PairStatistics() for category in _CATEGORIES}
    neighbor_scores = np.full((n, min(top_k, max(n - 1, 0))), -np.inf)
    neighbor_ids = np.full(neighbor_scores.shape, n, dtype=np.int64)
    for left in range(0, n, block_size):
        left_end = min(left + block_size, n)
        a = np.arange(left, left_end)
        for right in range(left, n, block_size):
            right_end = min(right + block_size, n)
            b = np.arange(right, right_end)
            scores = (matrix[left:left_end] @ matrix[right:right_end].T).toarray()
            # Only correct floating-point overshoot; normalized TF-IDF is nonnegative.
            np.clip(scores, 0.0, 1.0, out=scores)
            valid = valid_rows[a, None] & valid_rows[None, b]
            scores[~valid] = np.nan
            upper = a[:, None] < b[None, :]
            same_group = group_array[a, None] == group_array[None, b]
            exact = hash_array[a, None] == hash_array[None, b]
            masks = {
                "all": upper,
                "same_group": upper & same_group,
                "different_group": upper & ~same_group,
            }
            if has_splits:
                masks["cross_split"] = upper & (split_array[a, None] != split_array[None, b])
            for category, mask in masks.items():
                statistics[category].add(scores, mask, exact, left, right)
            candidates = np.where(valid & (a[:, None] != b[None, :]), scores, -np.inf)
            _merge_neighbors(neighbor_scores, neighbor_ids, left, candidates, b)
            if left != right:
                _merge_neighbors(neighbor_scores, neighbor_ids, right, candidates.T, a)
    pairs = {
        (min(i, int(j)), max(i, int(j)))
        for i in range(n)
        for score, j in zip(neighbor_scores[i], neighbor_ids[i], strict=True)
        if np.isfinite(score)
    }
    results = {key: value.result() for key, value in statistics.items()}
    if not has_splits:
        results["cross_split"] = None
    for result in results.values():
        if result is not None:
            pairs.update((item["left"], item["right"]) for item in result["top_pairs"])
    return {
        "empty_vector_indices": np.flatnonzero(~valid_rows).tolist(),
        "categories": results,
    }, pairs


def _pair_row(
    i: int,
    j: int,
    cases: Sequence[CaseDefinition],
    splits: Sequence[str | None],
    hashes: dict[str, list[str]],
    scores: dict[str, float | None],
) -> dict:
    a, b = cases[i], cases[j]
    row = {
        "case_a": a.id,
        "case_b": b.id,
        "group_a": a.group,
        "group_b": b.group,
        "split_a": splits[i],
        "split_b": splits[j],
        "primary_a": a.coverage.primary,
        "primary_b": b.coverage.primary,
        "archetype_a": a.coverage.archetype,
        "archetype_b": b.coverage.archetype,
        "same_group": a.group == b.group,
        "cross_split": splits[i] != splits[j] if splits[i] is not None else None,
    }
    for view, values in hashes.items():
        row[f"{view}_cosine"] = scores[view]
        row[f"{view}_exact"] = values[i] == values[j]
    return row


def _pair_rows(pairs, cases, splits, hashes, matrices, *, block_size=1024):
    for start in range(0, len(pairs), block_size):
        batch = pairs[start : start + block_size]
        left, right = np.array(batch).T
        scores = {}
        for view, matrix in matrices.items():
            a, b = matrix[left], matrix[right]
            values = np.asarray(a.multiply(b).sum(axis=1)).ravel()
            valid = (a.getnnz(axis=1) > 0) & (b.getnnz(axis=1) > 0)
            scores[view] = [
                float(np.clip(x, 0, 1)) if ok else None for x, ok in zip(values, valid, strict=True)
            ]
        for offset, (i, j) in enumerate(batch):
            yield _pair_row(
                i,
                j,
                cases,
                splits,
                hashes,
                {view: values[offset] for view, values in scores.items()},
            )


def _exact_families(cases, splits, hashes) -> dict:
    result = {}
    for view, values in hashes.items():
        buckets = defaultdict(list)
        for index, value in enumerate(values):
            buckets[value].append(index)
        result[view] = [
            {
                "view_hash": value,
                "cases": [cases[i].id for i in members],
                "groups": sorted({cases[i].group for i in members}),
                "splits": sorted({splits[i] for i in members if splits[i] is not None}),
                "pairs": len(members) * (len(members) - 1) // 2,
            }
            for value, members in sorted(buckets.items())
            if len(members) > 1
        ]
    return result


def _markdown(summary: dict) -> str:
    lines = [
        f"# Case similarity: {summary['skill']}",
        "",
        LIMITATION,
        "",
        f"Cases: {summary['case_count']:,}. Unique pairs: {summary['pair_count']:,}.",
        f"Audit: `{summary['audit_id']}`. Status: `{summary['status']}`.",
        "",
        "[JSON summary](summary.json) · [Nearest pairs](neighbors.csv) · "
        "[Exact duplicate families](exact-families.json)",
        "",
    ]
    for view, result in summary["views"].items():
        lines.extend(
            [
                f"## {view.capitalize()} view",
                "",
                "| Comparison | Pairs | Scorable | Exact inputs | Mean cosine |",
                "| --- | ---: | ---: | ---: | ---: |",
            ]
        )
        for category, stats in result["categories"].items():
            if stats is None:
                lines.append(f"| {category} | unavailable | | | |")
                continue
            mean = stats["mean_cosine"]
            lines.append(
                f"| {category} | {stats['pairs']:,} | {stats['scorable_pairs']:,} | "
                f"{stats['exact_pairs']:,} | {mean if mean is not None else 'unavailable'} |"
            )
        for category in ("cross_split", "different_group"):
            stats = result["categories"][category]
            if stats is None or not stats["top_pairs"]:
                continue
            lines.extend(
                [
                    "",
                    f"### Highest {category.replace('_', ' ')} similarities",
                    "",
                    "| Case A | Case B | Cosine |",
                    "| --- | --- | ---: |",
                ]
            )
            for row in stats["top_pairs"][:10]:
                lines.append(f"| {row['case_a']} | {row['case_b']} | {row['score']:.6f} |")
        lines.append("")
    return "\n".join(lines) + "\n"


def _publish(
    store: FilesystemArtifactStore, directory: Path, name: str, data: bytes, media_type: str
) -> dict:
    reference = store.put_blob(BytesIO(data), media_type)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / name
    if destination.is_symlink():
        raise IntegrityError(f"similarity report must not be a symlink: {destination}")
    if destination.exists() and destination.read_bytes() != data:
        raise IntegrityError(f"immutable similarity report differs: {destination}")
    if not destination.exists():
        temporary = directory / f".{name}.tmp"
        temporary.write_bytes(data)
        temporary.replace(destination)
    return json_value(reference)


def _audit_skill(
    repository,
    bank,
    catalog,
    split_manifest,
    store,
    skill,
    *,
    views,
    top_k,
    block_size,
    export_pairs,
    progress,
) -> dict:
    cases = sorted((case for case in bank.cases if case.skill == skill), key=lambda case: case.id)
    splits = [split_manifest.split_for(case) if split_manifest else None for case in cases]
    groups = [case.group for case in cases]
    identity = {
        "schema_version": 1,
        "skill": skill,
        "views": views,
        "cases": [(case.id, case.content_hash) for case in cases],
        "catalog_hash": catalog.content_hash,
        "split_hash": split_manifest.content_hash if split_manifest else None,
        "representation": REPRESENTATION,
    }
    audit_id = content_hash(identity)
    projection = {
        "audit_id": audit_id,
        "top_k": top_k,
        "export_pairs": export_pairs,
        "report_version": 1,
    }
    report_id = content_hash(projection)
    directory = (
        store.summary_root
        / "case-similarity"
        / audit_id.removeprefix("sha256:")
        / report_id.removeprefix("sha256:")
    )
    matrices, hashes, vector_manifests, results = {}, {}, {}, {}
    selected_pairs = set()
    for view in views:
        progress(f"{skill}: vectorizing {len(cases):,} {view} inputs")
        rendered = [render_view(case, view) for case in cases]
        texts = [text for text, _ in rendered]
        hashes[view] = [exact for _, exact in rendered]
        matrix, vector_manifest = _vectors(store, texts)
        matrices[view] = matrix
        vector_manifests[view] = vector_manifest
        progress(f"{skill}: comparing every {view} pair in blocks of {block_size}")
        results[view], selected = compare_vectors(
            matrix,
            groups,
            splits,
            hashes[view],
            top_k=top_k,
            block_size=block_size,
        )
        if split_manifest is None:
            results[view]["categories"]["cross_split"] = None
        selected_pairs.update(selected)
        for stats in results[view]["categories"].values():
            if stats is not None:
                for item in stats["top_pairs"]:
                    item["case_a"] = cases[item.pop("left")].id
                    item["case_b"] = cases[item.pop("right")].id
        results[view]["empty_vector_cases"] = [
            cases[i].id for i in results[view].pop("empty_vector_indices")
        ]
    manifest_reference = store.put_manifest(
        f"case-similarity/{audit_id.removeprefix('sha256:')}",
        {"identity": identity, "vectors": vector_manifests},
    )
    summary = {
        "schema_version": 1,
        "audit_id": audit_id,
        "report_id": report_id,
        "skill": skill,
        "case_count": len(cases),
        "pair_count": len(cases) * (len(cases) - 1) // 2,
        "status": "complete" if len(cases) > 1 else "insufficient_cases",
        "split_counts": dict(Counter(splits)) if split_manifest else None,
        "representation": REPRESENTATION,
        "limitation": LIMITATION,
        "top_k": top_k,
        "neighbor_pair_count": len(selected_pairs),
        "views": results,
    }
    assets = {
        "summary.json": _publish(
            store, directory, "summary.json", canonical_json_bytes(summary), "application/json"
        ),
        "summary.md": _publish(
            store, directory, "summary.md", _markdown(summary).encode(), "text/markdown"
        ),
        "exact-families.json": _publish(
            store,
            directory,
            "exact-families.json",
            canonical_json_bytes(_exact_families(cases, splits, hashes)),
            "application/json",
        ),
    }
    fields = [
        "case_a",
        "case_b",
        "group_a",
        "group_b",
        "split_a",
        "split_b",
        "primary_a",
        "primary_b",
        "archetype_a",
        "archetype_b",
        "same_group",
        "cross_split",
        *(field for view in views for field in (f"{view}_cosine", f"{view}_exact")),
    ]
    with TemporaryDirectory(dir=store.temporary_root, prefix="case-similarity-") as temp:
        for name in ["neighbors.csv", "pairs.csv"] if export_pairs else ["neighbors.csv"]:
            path = Path(temp) / name
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                if name == "neighbors.csv":
                    writer.writerows(
                        _pair_rows(sorted(selected_pairs), cases, splits, hashes, matrices)
                    )
                else:
                    # Stream one upper-triangle row at a time, never materialize all pairs.
                    for i in range(len(cases)):
                        writer.writerows(
                            _pair_rows(
                                [(i, j) for j in range(i + 1, len(cases))],
                                cases,
                                splits,
                                hashes,
                                matrices,
                            )
                        )
            with path.open("rb") as handle:
                reference = store.put_blob(handle, "text/csv")
            destination = directory / name
            if destination.is_symlink():
                raise IntegrityError(f"similarity report must not be a symlink: {destination}")
            if destination.exists():
                from .hashing import sha256_file

                if sha256_file(destination) != reference.content_id:
                    raise IntegrityError(f"immutable similarity report differs: {destination}")
            else:
                path.replace(destination)
            assets[name] = json_value(reference)
    store.put_manifest(
        f"case-similarity-reports/{report_id.removeprefix('sha256:')}",
        {"identity": projection, "audit": json_value(manifest_reference), "assets": assets},
    )
    progress(f"{skill}: complete; report {directory / 'summary.md'}")
    return {
        "skill": skill,
        "audit_id": audit_id,
        "report_id": report_id,
        "case_count": len(cases),
        "pair_count": summary["pair_count"],
        "status": summary["status"],
        "report": str(directory / "summary.md"),
        "summary": str(directory / "summary.json"),
        "neighbors": str(directory / "neighbors.csv"),
    }


def audit_case_similarity(
    repository: WorkspaceRepository,
    *,
    skills: Sequence[str] | None = None,
    view: str = "solver",
    top_k: int = 10,
    block_size: int = 256,
    export_pairs: bool = False,
    progress: Callable[[str], None] | None = None,
) -> dict:
    """Audit local case content without compilation, credentials, or model calls."""
    if top_k < 1 or block_size < 1:
        raise ConfigurationError("top-k and block-size must be positive")
    if view not in {"solver", "expected", "both"}:
        raise ConfigurationError(f"unknown similarity view: {view}")
    progress = progress or (lambda _: None)
    catalog = CapabilityCatalog.load(repository.capability_catalog_path)
    known = {item.skill for item in catalog.skills}
    selected = sorted(set(skills) if skills is not None else known)
    if not selected or set(selected) - known:
        raise ConfigurationError(f"unknown or empty similarity skill selection: {selected}")
    progress("Loading and verifying local case packages")
    bank = CaseBank.discover(repository.cases_root, require_cases=False)
    split_manifest = (
        SplitManifest.load(repository.split_file, bank) if repository.split_file.exists() else None
    )
    store = FilesystemArtifactStore(repository.data_root, workspace_root=repository.root)
    views = ["solver", "expected"] if view == "both" else [view]
    reports = [
        _audit_skill(
            repository,
            bank,
            catalog,
            split_manifest,
            store,
            skill,
            views=views,
            top_k=top_k,
            block_size=block_size,
            export_pairs=export_pairs,
            progress=progress,
        )
        for skill in selected
    ]
    bundle_id = content_hash([report["report_id"] for report in reports])
    directory = store.summary_root / "case-similarity" / bundle_id.removeprefix("sha256:")
    result = {
        "status": "complete",
        "data_root": str(store.data_root),
        "case_count": sum(report["case_count"] for report in reports),
        "pair_count": sum(report["pair_count"] for report in reports),
        "representation": "tfidf",
        "view": view,
        "reports": reports,
        "index": str(directory / "summary.md"),
    }
    markdown = [
        "# Case similarity audit",
        "",
        LIMITATION,
        "",
        "| Skill | Cases | Unique pairs | Report |",
        "| --- | ---: | ---: | --- |",
    ]
    for report in reports:
        markdown.append(
            f"| {report['skill']} | {report['case_count']:,} | "
            f"{report['pair_count']:,} | [Report]({report['report']}) |"
        )
    assets = {
        "summary": _publish(
            store, directory, "summary.md", ("\n".join(markdown) + "\n").encode(), "text/markdown"
        ),
        "result": _publish(
            store, directory, "summary.json", canonical_json_bytes(result), "application/json"
        ),
    }
    store.put_manifest(
        f"case-similarity-bundles/{bundle_id.removeprefix('sha256:')}",
        {"reports": reports, "assets": assets},
    )
    return result
