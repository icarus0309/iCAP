"""iCAP-main 自研测评引擎：Ruler1/Ruler2 尺子的规格、样例、判分与任务执行。"""
from .specs import RulerSpec, RULER1_SPEC, RULER2_SPEC, RULER_SPECS, RULER_IDS, get_ruler_spec
from .cases import get_cases_for_ruler
from .engine import score_case, flatten_cases, run_cases, build_result
from .client import demo_answer, live_answer, EndpointUnavailableError
from .worker import EvaluationRunner

__all__ = ["RulerSpec", "RULER1_SPEC", "RULER2_SPEC", "RULER_SPECS", "RULER_IDS", "get_ruler_spec",
           "get_cases_for_ruler", "score_case", "flatten_cases", "run_cases",
           "build_result", "demo_answer", "live_answer",
           "EvaluationRunner", "EndpointUnavailableError"]
