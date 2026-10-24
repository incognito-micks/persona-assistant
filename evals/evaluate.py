import argparse
import json
import logging
import os
import tempfile
from pathlib import Path

from azure.identity import AzureDeveloperCliCredential
from rich.logging import RichHandler

logger = logging.getLogger("ragapp")


def prepare_evaluation_config(testdata: Path) -> dict:
    testdata = testdata.resolve(strict=True)
    rows = [json.loads(line) for line in testdata.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows or any(
        not isinstance(row, dict)
        or not isinstance(row.get("question"), str)
        or not row["question"].strip()
        or not isinstance(row.get("truth"), str)
        or not row["truth"].strip()
        for row in rows
    ):
        raise ValueError("Evaluation data must contain JSONL objects with nonempty question and truth strings")
    config = json.loads((Path(__file__).parent / "evaluate_config.json").read_text(encoding="utf-8"))
    config["testdata_path"] = str(testdata)
    return config


def get_openai_config():
    return {
        "azure_endpoint": f"https://{os.getenv('AZURE_OPENAI_SERVICE')}.openai.azure.com",
        "azure_deployment": os.environ["AZURE_OPENAI_EVAL_DEPLOYMENT"],
    }


def get_azure_credential():
    tenant_id = os.getenv("AZURE_TENANT_ID")
    return AzureDeveloperCliCredential(tenant_id=tenant_id, process_timeout=60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Evaluate personal chat against the current knowledge set.")
    parser.add_argument("--testdata", type=Path, required=True, help="JSONL objects with question and truth strings.")
    parser.add_argument("--targeturl", type=str, help="Specify the target URL.")
    parser.add_argument("--resultsdir", type=Path, help="Specify the results directory.")
    parser.add_argument("--numquestions", type=int, help="Specify the number of questions.")
    args = parser.parse_args()
    config = prepare_evaluation_config(args.testdata)

    from dotenv_azd import load_azd_env
    from evaltools.eval.evaluate import run_evaluate_from_config

    logging.basicConfig(
        level=logging.WARNING, format="%(message)s", datefmt="[%X]", handlers=[RichHandler(rich_tracebacks=True)]
    )
    logger.setLevel(logging.INFO)
    logging.getLogger("evaltools").setLevel(logging.INFO)
    load_azd_env()

    with tempfile.TemporaryDirectory(prefix="personal-chat-evaluation-") as temporary_dir:
        config_path = Path(temporary_dir) / "evaluate_config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        run_evaluate_from_config(
            working_dir=Path(__file__).parent,
            config_path=str(config_path),
            num_questions=args.numquestions,
            target_url=args.targeturl,
            results_dir=args.resultsdir,
            openai_config=get_openai_config(),
            model=os.environ["AZURE_OPENAI_EVAL_MODEL"],
            azure_credential=get_azure_credential(),
        )
