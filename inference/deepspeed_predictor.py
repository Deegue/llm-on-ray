import ray
import torch
import oneccl_bindings_for_pytorch
import deepspeed

from transformers import AutoModelForCausalLM, AutoTokenizer
from ray.air.util.torch_dist import (
    TorchDistributedWorker,
    init_torch_dist_process_group,
    shutdown_torch_dist_process_group,
)
from ray.util.scheduling_strategies import PlacementGroupSchedulingStrategy
from ray.air import Checkpoint, ScalingConfig
from typing import List
import os
# import math

class DSPipeline:
    def __init__(
        self,
        model_id_or_path,
        trust_remote_code,
        pad_token_id,
        stopping_criteria,
        dtype,
        device
    ):

        self.dtype = dtype
        self.device = device
        self.pad_token_id = pad_token_id
        self.stopping_criteria = stopping_criteria

        self.model = AutoModelForCausalLM.from_pretrained(model_id_or_path,
                                                          torch_dtype=dtype,
                                                          low_cpu_mem_usage=True,
                                                          trust_remote_code=trust_remote_code)

        self.model = self.model.eval().to(device)
        # to channels last
        self.model = self.model.to(memory_format=torch.channels_last)
        self.model.eval()

    def streaming_generate(self, input_ids, streamer, **generate_kwargs):
        self.model.generate(input_ids,
                    pad_token_id=self.pad_token_id,
                    stopping_criteria=self.stopping_criteria,
                    streamer=streamer,
                    **generate_kwargs)

    def generate(self, input_ids, **config):
        gen_tokens = self.model.generate(
            input_ids,
            pad_token_id=self.pad_token_id,
            stopping_criteria=self.stopping_criteria,
            **config
        )
        return gen_tokens

@ray.remote
class PredictionWorker(TorchDistributedWorker):
    """A PredictionWorker is a Ray remote actor that runs a single shard of a DeepSpeed job.

    Multiple PredictionWorkers of the same WorkerGroup form a PyTorch DDP process
    group and work together under the orchestration of DeepSpeed.
    """
    def __init__(self, world_size: int, model_id, trust_remote_code, device_name, amp_enabled, amp_dtype, pad_token_id, stopping_criteria, streamer=None, ipex_enabled=False):
        self.world_size = world_size
        self.model_id = model_id
        self.trust_remote_code = trust_remote_code
        self.device_name = device_name
        self.device = torch.device(self.device_name)
        self.amp_enabled = amp_enabled
        self.amp_dtype = amp_dtype
        self.pad_token_id = pad_token_id
        self.stopping_criteria = stopping_criteria
        self.streamer = streamer
        self.ipex_enabled = ipex_enabled

    def init_model(self, local_rank: int):
        """Initialize model for inference."""

        if self.device_name == 'cpu':
            replace_with_kernel_inject = False
        elif self.device_name == 'xpu':
            replace_with_kernel_inject = False
        else:
            replace_with_kernel_inject = True

        os.environ['LOCAL_RANK'] = str(local_rank)
        os.environ['WORLD_SIZE'] = str(self.world_size)

        pipe = DSPipeline(
            model_id_or_path=self.model_id,
            trust_remote_code=self.model_id,
            pad_token_id=self.pad_token_id,
            stopping_criteria=self.stopping_criteria,
            dtype=self.amp_dtype,
            device=self.device
        )

        pipe.model = deepspeed.init_inference(
            pipe.model,
            dtype=self.amp_dtype,
            mp_size=self.world_size,
            replace_with_kernel_inject=replace_with_kernel_inject
        )

        if self.ipex_enabled:
            import intel_extension_for_pytorch as ipex
            try: ipex._C.disable_jit_linear_repack()
            except: pass
            pipe.model = ipex.optimize(pipe.model.eval(), dtype=self.amp_dtype, inplace=True)

        self.generator = pipe

    def streaming_generate(self, input_ids, **config):
        self.generator.streaming_generate(input_ids, self.streamer, **config)

    def generate(self, input_ids, **config):
        return self.generator.generate(input_ids, **config)

class DeepSpeedPredictor:
    def __init__(self, model_id, trust_remote_code, device_name, amp_enabled, amp_dtype, pad_token_id, stopping_criteria, streamer,
                 ipex_enabled, cpus_per_worker, workers_per_group) -> None:
        self.model_id = model_id
        self.trust_remote_code = trust_remote_code
        self.device_name = device_name
        self.amp_enabled = amp_enabled
        self.amp_dtype = amp_dtype
        self.pad_token_id = pad_token_id
        self.stopping_criteria = stopping_criteria
        self.streamer = streamer
        self.ipex_enabled = ipex_enabled

        use_gpu = True if (device_name == "xpu") else False

        # Scaling config for one worker group.
        scaling_conf = ScalingConfig(
            use_gpu=use_gpu,
            num_workers=workers_per_group,
            resources_per_worker={"CPU": cpus_per_worker},
        )

        print(scaling_conf)

        self._init_worker_group(scaling_conf)

    def __del__(self):
        shutdown_torch_dist_process_group(self.prediction_workers)

    # Use dummy streamer to ignore other workers' ouputs
    def _create_dummy_streamer(self):
        class DummyStreamer():
            def put(self, value):
                pass

            def end(self):
                pass

        return DummyStreamer()

    def _init_worker_group(self, scaling_config: ScalingConfig):
        """Create the worker group.

        Each worker in the group communicates with other workers through the
        torch distributed backend. The worker group is inelastic (a failure of
        one worker destroys the entire group). Each worker in the group
        recieves the same input data and outputs the same generated text.
        """

        # Start a placement group for the workers.
        self.pg = scaling_config.as_placement_group_factory().to_placement_group()
        prediction_worker_cls = PredictionWorker.options(
            num_cpus=scaling_config.num_cpus_per_worker,
            scheduling_strategy=PlacementGroupSchedulingStrategy(
                placement_group=self.pg, placement_group_capture_child_tasks=True
            ),
        )

        # Create the prediction workers.
        self.prediction_workers = [
            prediction_worker_cls.remote(scaling_config.num_workers, self.model_id, self.trust_remote_code, self.
                device_name, self.amp_enabled, self.amp_dtype,
                self.pad_token_id, self.stopping_criteria, self.streamer, self.ipex_enabled)
            if i == 0 else
                prediction_worker_cls.remote(scaling_config.num_workers, self.model_id, self.trust_remote_code, self.
                    device_name, self.amp_enabled, self.amp_dtype,
                    self.pad_token_id, self.stopping_criteria, self._create_dummy_streamer(), self.ipex_enabled)
            for i in range(scaling_config.num_workers)
        ]

        # Initialize torch distributed process group for the workers.
        local_ranks = init_torch_dist_process_group(self.prediction_workers, backend="ccl")

        # Initialize the model on each worker.
        ray.get([
            worker.init_model.remote(local_rank)
            for worker, local_rank in zip(self.prediction_workers, local_ranks)
        ])

    def streaming_generate(self, input_ids, **config):
        input_ids_ref = ray.put(input_ids)
        ray.get(
            [
                worker.streaming_generate.remote(input_ids_ref, **config)
                for worker in self.prediction_workers
            ]
        )

    def generate(self, input_ids, **config):
        input_ids_ref = ray.put(input_ids)
        prediction = ray.get(
            [
                worker.generate.remote(input_ids_ref, **config)
                for worker in self.prediction_workers
            ]
        )[0]
        return prediction

    def predict(
        self,
        data: List[str],
        **kwargs
    ) -> str:
        data_ref = ray.put(data)
        prediction = ray.get(
            [
                worker.generate.remote(data_ref, **kwargs)
                for worker in self.prediction_workers
            ]
        )

        return prediction