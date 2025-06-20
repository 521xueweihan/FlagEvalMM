import re
import os
import os.path as osp
import json
from typing import Dict, Any, Optional, Union, List
from concurrent.futures import ThreadPoolExecutor, as_completed
import atexit
import signal
from importlib.metadata import version, PackageNotFoundError

from flagevalmm.server import ServerDataset
from flagevalmm.models.base_model_adapter import BaseModelAdapter
from flagevalmm.models import HttpClient, Claude, Gemini, GPT, Hunyuan
from flagevalmm.server.model_server import ModelServer
from flagevalmm.server.utils import get_random_port
from flagevalmm.common.logger import get_logger
from flagevalmm.server.utils import parse_args

logger = get_logger(__name__)


class ModelAdapter(BaseModelAdapter):
    def __init__(
        self,
        server_ip: str,
        server_port: int,
        timeout: int,
        model_type: Optional[str] = None,
        extra_cfg: Optional[Union[str, Dict]] = None,
        local_mode: bool = False,
        task_names: List[str] = None,
        **kwargs,
    ):
        self.model_type = model_type
        super().__init__(
            server_ip=server_ip,
            server_port=server_port,
            timeout=timeout,
            extra_cfg=extra_cfg,
            local_mode=local_mode,
            task_names=task_names,
            **kwargs,
        )

        atexit.register(self.cleanup)
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    def _signal_handler(self, signum, frame):
        logger.info(f"Received signal {signum}, cleaning up...")
        self.cleanup()
        os._exit(0)

    def model_init(self, task_info: Dict):
        if task_info.get("backend", None):
            self.model_server = self.launch_model(task_info)

        model_config_keys = [
            "model_name",
            "url",
            "base_url",
            "api_key",
            "use_cache",
            "max_image_size",
            "min_short_side",
            "max_long_side",
            "max_tokens",
            "temperature",
            "chat_name",
            "max_num_frames",
            "stream",
            "system_prompt",
            "num_infers",
        ]
        print(f"task_info: {task_info}")
        model_config = {k: task_info[k] for k in model_config_keys if k in task_info}

        model_type_map = {
            "http": HttpClient,
            "claude": Claude,
            "gemini": Gemini,
            "gpt": GPT,
            "hunyuan": Hunyuan,
        }
        model_type = self.model_type or task_info.get("model_type", "http")
        self.model = model_type_map[model_type](**model_config)

    def launch_model(self, task_info: Dict):
        if task_info.get("server_port"):
            port = task_info.get("server_port")
        else:
            port = get_random_port()
        # replace port in url
        url = re.sub(
            r":(\d+)/",
            f":{port}/",
            task_info.get("url", "http://localhost:8000/v1/chat/completions"),
        )
        task_info["url"] = url

        model_name = task_info["model_name"]
        backend = task_info.get("backend", "vllm")
        model_server = ModelServer(
            model_name,
            port=port,
            backend=backend,
            extra_args=task_info.get("extra_args", None),
        )
        task_info["execute_cmd"] = model_server.execute_cmd
        important_packages = [backend, "transformers", "torch"]
        task_info["important_packages"] = []
        for package in important_packages:
            try:
                version_pkg = version(package)
                task_info["important_packages"].append(f"{package}=={version_pkg}")
            except PackageNotFoundError:
                task_info["important_packages"].append(f"{package} not installed")
        return model_server

    def process_single_item(self, i, inter_results_dir):
        question_id, multi_modal_data, qs = self.dataset[i]
        inter_results_file = osp.join(inter_results_dir, f"{question_id}.json")
        if osp.exists(inter_results_file):
            logger.info(f"Skipping {question_id} because it already exists")
            with open(inter_results_file, "r") as f:
                data = json.load(f)
                reason = data.get("reason", "")
                result = data.get("answer", "")
                multiple_raw_answers = data.get("multiple_raw_answers", [])
                return {
                    "question_id": question_id,
                    "question": qs,
                    "answer": result,
                    "reason": reason,
                    "multiple_raw_answers": multiple_raw_answers,
                }
        logger.info(f"Processing {question_id}")
        logger.info(qs)
        messages = self.model.build_message(qs, multi_modal_data=multi_modal_data)
        reason = ""
        multiple_raw_answers = {}

        try:
            result = self.model.infer(messages)

            if isinstance(result, list):
                multiple_raw_answers = {}
                processed_results = []
                for i, single_result in enumerate(result):
                    if "</think>" in single_result:
                        single_reason, single_answer = single_result.split(
                            "</think>", 1
                        )
                        single_reason += "</think>"
                        if not reason:
                            reason = single_reason
                    else:
                        single_answer = single_result
                    multiple_raw_answers[f"inference_{i}"] = single_answer
                    processed_results.append(single_answer)
                result = multiple_raw_answers
                logger.info(
                    f"Multiple inferences completed. Got {len(multiple_raw_answers)} results."
                )
            else:
                # single inference
                if "</think>" in result:
                    reason, result = result.split("</think>", 1)
                    reason += "</think>"
                multiple_raw_answers = [result]

        except Exception as e:
            result = "Error code " + str(e)
            multiple_raw_answers = [result]

        return {
            "question_id": question_id,
            "question": qs,
            "answer": result,
            "reason": reason,
            "multiple_raw_answers": multiple_raw_answers,
        }

    def cleanup(self):
        if hasattr(self, "model_server") and self.model_server is not None:
            try:
                self.model_server.stop()
                self.model_server = None
            except Exception as e:
                logger.error(f"Error shutting down model server: {e}")

    def run_one_task(self, task_name: str, meta_info: Dict[str, Any]):
        self.dataset = ServerDataset(
            task_name,
            task_type=meta_info["type"],
            task_manager=self.task_manager,
        )

        results = []
        num_workers = self.task_info.get("num_workers", 1)
        inter_results_dir = osp.join(meta_info["output_dir"], "items")
        os.makedirs(inter_results_dir, exist_ok=True)
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            future_to_item = {
                executor.submit(self.process_single_item, i, inter_results_dir): i
                for i in range(len(self.dataset))
            }

            for future in as_completed(future_to_item):
                result = future.result()
                if isinstance(result["answer"], str) and result["answer"].startswith(
                    "Error code"
                ):
                    continue
                else:
                    self.save_item(result, result["question_id"], meta_info)
                results.append(result)
        self.save_result(results, meta_info)


if __name__ == "__main__":
    args = parse_args()
    model_adapter = ModelAdapter(
        server_ip=args.server_ip,
        server_port=args.server_port,
        timeout=args.timeout,
        model_type=args.model_type,
        extra_cfg=args.cfg,
        local_mode=args.local_mode,
        task_names=args.tasks,
        output_dir=args.output_dir,
        model_path=args.model,
        debug=args.debug,
        quiet=args.quiet,
    )
    model_adapter.run()
