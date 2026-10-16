"""Docker recipe for SWE-bench Lite prebaked images."""

from __future__ import annotations

from copy import deepcopy

from agentcompass.benchmarks.swebench_lite import SWEBenchLiteBenchmarkPlan
from agentcompass.runtime import RECIPES, BaseRecipe, ExecutionPlan, RunRequest, TaskSpec


@RECIPES.register()
class SWEBenchLiteDockerRecipe(BaseRecipe):
    id = "swebench_lite_docker_prebaked"

    def matches(self, req: RunRequest, task: TaskSpec, plan: ExecutionPlan) -> bool:
        if req.benchmark.id != "swebench_lite" or req.environment.id != "docker":
            return False
        if req.environment.params.get("image"):
            return False
        return True

    @staticmethod
    def get_image_tag(instance: dict) -> str:
        instance_id = instance.get("instance_id")
        if instance_id is None:
            raise Exception("For SWE-Bench Lite, instance_id is required.")
        tag = f"sweb.eval.x86_64.{instance_id.replace('__', '_1776_')}"
        return f"swebench/{tag}"

    def apply(self, plan: ExecutionPlan, req: RunRequest, task: TaskSpec) -> ExecutionPlan:
        updated_plan = deepcopy(plan)
        if not isinstance(updated_plan.benchmark_plan, SWEBenchLiteBenchmarkPlan):
            raise TypeError("swebench_lite_docker_prebaked requires SWEBenchLiteBenchmarkPlan")

        updated_plan.environment.params["image"] = self.get_image_tag(task.metadata)

        benchmark_plan = updated_plan.benchmark_plan
        benchmark_plan.prepare_mode = "prebaked"
        benchmark_plan.workspace_dir = "/testbed"
        benchmark_plan.repo_dir = "/testbed"
        benchmark_plan.evaluation_prepare_mode = "prebaked"
        benchmark_plan.evaluation_workspace_dir = "/testbed"
        benchmark_plan.evaluation_repo_dir = "/testbed"

        return updated_plan
