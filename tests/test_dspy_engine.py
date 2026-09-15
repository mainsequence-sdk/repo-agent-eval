from __future__ import annotations

import json

from ms_agent_eval.programs.dspy.engine import CoproProposal, _PortableStructuredAdapter


def _candidate() -> dict[str, str]:
    return {"instruction": "Complete rewritten skill", "output_prefix": ""}


def test_portable_adapter_accepts_chat_adapter_markers() -> None:
    completion = (
        "[[ ## candidates ## ]]\n"
        + json.dumps([_candidate()])
        + "\n\n[[ ## completed ## ]]"
    )

    assert _PortableStructuredAdapter().parse(CoproProposal, completion) == {
        "candidates": [_candidate()]
    }


def test_portable_adapter_accepts_canonical_json_object() -> None:
    completion = json.dumps({"candidates": [_candidate()]})

    assert _PortableStructuredAdapter().parse(CoproProposal, completion) == {
        "candidates": [_candidate()]
    }


def test_portable_adapter_accepts_single_output_json_array() -> None:
    completion = json.dumps([_candidate()])

    assert _PortableStructuredAdapter().parse(CoproProposal, completion) == {
        "candidates": [_candidate()]
    }


def test_portable_adapter_accepts_explanation_before_fenced_json_array() -> None:
    candidate = {
        "instruction": "Complete rewrite\n```yaml\nkey: value\n```",
        "output_prefix": "",
    }
    completion = (
        "I checked the constraints and prepared one complete candidate.\n\n"
        "```json\n"
        + json.dumps([candidate])
        + "\n```"
    )

    assert _PortableStructuredAdapter().parse(CoproProposal, completion) == {
        "candidates": [candidate]
    }
