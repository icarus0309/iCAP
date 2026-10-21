"""One logical execution-model call, with no agent or environment hooks."""
import copy
import json
from pathlib import Path
from ...model_client import QuotaExceededError
from ..schemas import HermesRunResult, to_plain_dict
from ..evaluation_policy import model_identity, write_json


class DirectRunner:
    def __init__(self, model_client, run_dir=None, source="direct", logger=None, model_role="small"):
        self.model_client = model_client
        self.source = source
        self.model_role = model_role

    def run_case(self, *, case_id, skill_path, input_messages, case_dir, **unused):
        case_dir = Path(case_dir)
        case_dir.mkdir(parents=True, exist_ok=True)
        output, error = "", ""
        try:
            skill = Path(skill_path).read_text(encoding="utf-8")
            if not skill.strip() or not input_messages:
                raise ValueError("empty SKILL or input messages")
            if self.model_client is None:
                raise ValueError("missing direct execution client")
            output = self.model_client.generate([{"role": "system", "content": skill}, *copy.deepcopy(input_messages)])
            if not isinstance(output, str) or not output.strip():
                raise ValueError("execution model returned empty output")
        except QuotaExceededError:
            raise
        except Exception as exc:
            output, error = "", str(exc)
        model = model_identity(self.model_client).get("model_name", "")
        raw_path = (case_dir / "session.raw.jsonl").resolve()
        raw_path.write_text(json.dumps({"case_id": case_id, "execution_mode": "direct",
            "model_role": self.model_role, "messages": [*input_messages, {"role": "assistant", "content": output}]},
            ensure_ascii=False) + "\n", encoding="utf-8")
        result = HermesRunResult(case_id=case_id, command=["direct-llm", model], returncode=1 if error else 0,
            stdout=output, stderr=error, source=self.source, hermes_home="", raw_session_path=str(raw_path),
            final_output=output, error=error, execution_mode="direct", model_role=self.model_role, model_name=model)
        write_json(case_dir / "run_result.json", to_plain_dict(result))
        return result
