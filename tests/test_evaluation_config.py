import json

import pytest
from evals.evaluate import prepare_evaluation_config


def test_personal_evaluation_uses_supplied_dataset_and_active_chat_api(tmp_path):
    dataset = tmp_path / "questions.jsonl"
    dataset.write_text(
        json.dumps({"question": "What do you build?", "truth": "I build AI tools."}) + "\n", encoding="utf-8"
    )

    config = prepare_evaluation_config(dataset)

    assert config["testdata_path"] == str(dataset.resolve())
    assert config["target_url"].endswith("/chat")
    assert set(config["requested_metrics"]) == {"gpt_groundedness", "gpt_relevance", "answer_length", "latency"}
    assert set(config["target_parameters"]["overrides"]) == {
        "temperature",
        "suggest_followup_questions",
        "language",
        "seed",
    }


@pytest.mark.parametrize("content", ["", "[]\n", '{"question":"hello"}\n', '{"question":"","truth":"answer"}\n'])
def test_personal_evaluation_rejects_invalid_datasets(tmp_path, content):
    dataset = tmp_path / "questions.jsonl"
    dataset.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="question and truth"):
        prepare_evaluation_config(dataset)
