import asyncio
import time
from typing import TYPE_CHECKING, Any, ClassVar, Dict, Iterable, List, Optional, Type, Union

from joblib import parallel_config
from transformers import GenerationConfig, PreTrainedTokenizer

import vllm
from vllm.config import (CacheConfig, DecodingConfig, DeviceConfig, LoadConfig,
                         LoRAConfig, ModelConfig, MultiModalConfig,
                         ObservabilityConfig, ParallelConfig,
                         PromptAdapterConfig, SchedulerConfig,
                         SpeculativeConfig)
from vllm.core.scheduler import Scheduler, SchedulerOutputs
from vllm.engine.arg_utils import EngineArgs
from vllm.pooling_params import PoolingParams
from vllm.prompt_adapter.request import PromptAdapterRequest
from vllm.engine.metrics import (LoggingStatLogger, PrometheusStatLogger,
                                 StatLoggerBase, Stats)
from vllm.inputs import INPUT_REGISTRY, LLMInputs, PromptInputs
from vllm.engine.output_processor.interfaces import (
    SequenceGroupOutputProcessor)
from vllm.engine.output_processor.stop_checker import StopChecker
from vllm.engine.output_processor.util import create_output_by_sequence_group
from vllm.executor.ray_utils import initialize_ray_cluster
from vllm.executor.executor_base import ExecutorBase
from vllm.logger import init_logger
from vllm.lora.request import LoRARequest
from vllm.outputs import RequestOutput,RequestOutputFactory, AdditionalInfo
from vllm.sampling_params import SamplingParams
from vllm.sequence import (ExecuteModelRequest, SamplerOutput, Sequence,
                           SequenceGroup)
from vllm.transformers_utils.detokenizer import Detokenizer
from vllm.transformers_utils.tokenizer_group import (BaseTokenizerGroup,
                                                     get_tokenizer_group)
from vllm.usage.usage_lib import (UsageContext, is_usage_stats_enabled,
                                  usage_message)
from vllm.utils import Counter

logger = init_logger(__name__)
_LOCAL_LOGGING_INTERVAL_SEC = 5


def _load_generation_config_dict(model_config: ModelConfig):
    try:
        return GenerationConfig.from_pretrained(
            model_config.model,
            revision=model_config.revision,
        ).to_diff_dict()
    except OSError:
        # Not found.
        return {}

class AUXLLMEngine:
    """An LLM engine that receives requests and generates texts.

    This is the main class for the vLLM engine. It receives requests
    from clients and generates texts from the LLM. It includes a tokenizer, a
    language model (possibly distributed across multiple GPUs), and GPU memory
    space allocated for intermediate states (aka KV cache). This class utilizes
    iteration-level scheduling and efficient memory management to maximize the
    serving throughput.

    The `LLM` class wraps this class for offline batched inference and the
    `AsyncLLMEngine` class wraps this class for online serving.

    NOTE: The config arguments are derived from the `EngineArgs` class. For the
    comprehensive list of arguments, see `EngineArgs`.

    Args:
        model_config: The configuration related to the LLM model.
        cache_config: The configuration related to the KV cache memory
            management.
        parallel_config: The configuration related to distributed execution.
        scheduler_config: The configuration related to the request scheduler.
        device_config: The configuration related to the device.
        lora_config (Optional): The configuration related to serving multi-LoRA.
        vision_language_config (Optional): The configuration related to vision
            language models.
        speculative_config (Optional): The configuration related to speculative
            decoding.
        executor_class: The model executor class for managing distributed
            execution.
        log_stats: Whether to log statistics.
        usage_context: Specified entry point, used for usage info collection
    """

    def __init__(
        self,
        model_config: ModelConfig,
        cache_config: CacheConfig,
        parallel_config: ParallelConfig,
        scheduler_config: SchedulerConfig,
        device_config: DeviceConfig,
        load_config: LoadConfig,
        lora_config: Optional[LoRAConfig],
        multimodal_config: Optional[MultiModalConfig],
        speculative_config: Optional[SpeculativeConfig],
        decoding_config: Optional[DecodingConfig],
        observability_config: Optional[ObservabilityConfig],
        prompt_adapter_config: Optional[PromptAdapterConfig],
        executor_class: Type[ExecutorBase],
        log_stats: bool,
        usage_context: UsageContext = UsageContext.ENGINE_CONTEXT,
        stat_loggers: Optional[Dict[str, StatLoggerBase]] = None,
    ) -> None:
        logger.info(f"Loading aux model={model_config.model!r}")

        logger.debug(
            "Initializing an LLM engine  with config: "
            "model=%r, speculative_config=%r, tokenizer=%r, "
            "skip_tokenizer_init=%s, tokenizer_mode=%s, revision=%s, "
            "rope_scaling=%r, rope_theta=%r, tokenizer_revision=%s, "
            "trust_remote_code=%s, dtype=%s, max_seq_len=%d, "
            "download_dir=%r, load_format=%s, tensor_parallel_size=%d, "
            "pipeline_parallel_size=%d, "
            "disable_custom_all_reduce=%s, quantization=%s, "
            "enforce_eager=%s, kv_cache_dtype=%s, "
            "quantization_param_path=%s, device_config=%s, "
            "decoding_config=%r, observability_config=%r, "
            "seed=%d, served_model_name=%s, use_v2_block_manager=%s, "
            "enable_prefix_caching=%s)",
            model_config.model,
            speculative_config,
            model_config.tokenizer,
            model_config.skip_tokenizer_init,
            model_config.tokenizer_mode,
            model_config.revision,
            model_config.rope_scaling,
            model_config.rope_theta,
            model_config.tokenizer_revision,
            model_config.trust_remote_code,
            model_config.dtype,
            model_config.max_model_len,
            load_config.download_dir,
            load_config.load_format,
            parallel_config.tensor_parallel_size,
            parallel_config.pipeline_parallel_size,
            parallel_config.disable_custom_all_reduce,
            model_config.quantization,
            model_config.enforce_eager,
            cache_config.cache_dtype,
            model_config.quantization_param_path,
            device_config.device,
            decoding_config,
            observability_config,
            model_config.seed,
            model_config.served_model_name,
            scheduler_config.use_v2_block_manager,
            cache_config.enable_prefix_caching,
        )

        self.model_config = model_config
        self.cache_config = cache_config
        self.lora_config = lora_config
        self.parallel_config = parallel_config
        self.scheduler_config = scheduler_config
        self.device_config = device_config
        self.speculative_config = speculative_config
        self.load_config = load_config
        self.multimodal_config = multimodal_config
        self.decoding_config = decoding_config or DecodingConfig()
        self.log_stats = log_stats

        self.tokenizer = self._init_tokenizer()
        self.detokenizer = Detokenizer(self.tokenizer)
        self.seq_counter = Counter()
        self.generation_config_fields = _load_generation_config_dict(
            model_config)

        
        self.model_executor= executor_class(
            model_config=model_config,
            cache_config=cache_config,
            parallel_config=parallel_config,
            scheduler_config=scheduler_config,
            device_config=device_config,
            lora_config=lora_config,
            multimodal_config=multimodal_config,
            speculative_config=speculative_config,
            load_config=load_config,
            prompt_adapter_config=prompt_adapter_config,
            is_aux_model=True,
        )
        
        self._initialize_kv_caches()

        # If usage stat is enabled, collect relevant info.
        if is_usage_stats_enabled():
            from vllm.model_executor.model_loader import (
                get_architecture_class_name)
            usage_message.report_usage(
                get_architecture_class_name(model_config),
                usage_context,
                extra_kvs={
                    # Common configuration
                    "dtype":
                    str(model_config.dtype),
                    "tensor_parallel_size":
                    parallel_config.tensor_parallel_size,
                    "block_size":
                    cache_config.block_size,
                    "gpu_memory_utilization":
                    cache_config.gpu_memory_utilization,

                    # Quantization
                    "quantization":
                    model_config.quantization,
                    "kv_cache_dtype":
                    cache_config.cache_dtype,

                    # Feature flags
                    "enable_lora":
                    bool(lora_config),
                    "enable_prefix_caching":
                    cache_config.enable_prefix_caching,
                    "enforce_eager":
                    model_config.enforce_eager,
                    "disable_custom_all_reduce":
                    parallel_config.disable_custom_all_reduce,
                })

        # Ping the tokenizer to ensure liveness if it runs in a
        # different process.
        self.tokenizer.ping()

        # Create the scheduler.
        # NOTE: the cache_config here have been updated with the numbers of
        # GPU and CPU blocks, which are profiled in the distributed executor.
        self.virtual_engine = 0
        scheduler_config.fake_allocate = True
        self.scheduler = [
            Scheduler(scheduler_config, cache_config, lora_config,
                      parallel_config.pipeline_parallel_size)
            for _ in range(parallel_config.pipeline_parallel_size)
        ]

        # Metric Logging.
        if self.log_stats:
            if stat_loggers is not None:
                self.stat_loggers = stat_loggers
            else:
                self.stat_loggers = {
                    "logging":
                    LoggingStatLogger(
                        local_interval=_LOCAL_LOGGING_INTERVAL_SEC),
                    "prometheus":
                    PrometheusStatLogger(
                        local_interval=_LOCAL_LOGGING_INTERVAL_SEC,
                        labels=dict(model_name=model_config.served_model_name),
                        max_model_len=self.model_config.max_model_len),
                }
                self.stat_loggers["prometheus"].info("cache_config",
                                                     self.cache_config)

        # Create sequence output processor, e.g. for beam search or
        # speculative decoding.
        self.output_processor = (
            SequenceGroupOutputProcessor.create_output_processor(
                self.scheduler_config,
                self.detokenizer,
                self.scheduler,
                self.seq_counter,
                self.get_tokenizer_for_seq,
                stop_checker=StopChecker(
                    self.scheduler_config.max_model_len,
                    self.get_tokenizer_for_seq,
                ),
            ))

    def _initialize_kv_caches(self) -> None:
        """Initialize the KV cache in the worker(s).

        The workers will determine the number of blocks in both the GPU cache
        and the swap CPU cache.
        """
        num_gpu_blocks, num_cpu_blocks = (
            self.model_executor.determine_num_available_blocks(is_aux_model=True))

        if self.cache_config.num_gpu_blocks_override is not None:
            num_gpu_blocks_override = self.cache_config.num_gpu_blocks_override
            logger.info(f"Overriding {num_gpu_blocks=} with "
                        f"{num_gpu_blocks_override=}")
            num_gpu_blocks = num_gpu_blocks_override

        self.cache_config.num_gpu_blocks = num_gpu_blocks
        self.cache_config.num_cpu_blocks = num_cpu_blocks

        self.model_executor.initialize_cache_empty(num_gpu_blocks, num_cpu_blocks)

    @classmethod
    def from_engine_args(
        cls,
        engine_args: EngineArgs,
        usage_context: UsageContext = UsageContext.ENGINE_CONTEXT,
    ) -> "LLMEngine":
        """Creates an LLM engine from the engine arguments."""
        # Create the engine configs.
        engine_config = engine_args.create_engine_config()
        distributed_executor_backend = (
            engine_config.parallel_config.distributed_executor_backend)
        # Initialize the cluster and specify the executor class.
        if engine_config.device_config.device_type == "neuron":
            from vllm.executor.neuron_executor import NeuronExecutor
            executor_class = NeuronExecutor
        elif engine_config.device_config.device_type == "tpu":
            from vllm.executor.tpu_executor import TPUExecutor
            executor_class = TPUExecutor
        elif engine_config.device_config.device_type == "cpu":
            from vllm.executor.cpu_executor import CPUExecutor
            executor_class = CPUExecutor
        elif engine_config.device_config.device_type == "openvino":
            from vllm.executor.openvino_executor import OpenVINOExecutor
            executor_class = OpenVINOExecutor
        elif engine_config.device_config.device_type == "xpu":
            if distributed_executor_backend == "ray":
                initialize_ray_cluster(engine_config.parallel_config)
                from vllm.executor.ray_xpu_executor import RayXPUExecutor
                executor_class = RayXPUExecutor
            else:
                from vllm.executor.xpu_executor import XPUExecutor
                executor_class = XPUExecutor
        elif distributed_executor_backend == "ray":
            initialize_ray_cluster(engine_config.parallel_config)
            from vllm.executor.ray_gpu_executor import RayGPUExecutor
            executor_class = RayGPUExecutor
            if engine_config.parallel_config.pipeline_parallel_size > 1:
                from vllm.executor.gpu_executor import GPUExecutor
                executor_class = GPUExecutor 
        elif distributed_executor_backend == "mp":
            from vllm.executor.multiproc_gpu_executor import (
                MultiprocessingGPUExecutor)
            executor_class = MultiprocessingGPUExecutor
        else:
            from vllm.executor.gpu_executor import GPUExecutor
            executor_class = GPUExecutor
        # Create the LLM engine.
        engine = cls(
            **engine_config.to_dict(),
            executor_class=executor_class,
            log_stats=not engine_args.disable_log_stats,
            usage_context=usage_context,
        )
        return engine

    def __reduce__(self):
        # This is to ensure that the LLMEngine is not referenced in
        # the closure used to initialize Ray worker actors
        raise RuntimeError("AUXLLMEngine should not be pickled!")
    def __del__(self):
        # Shutdown model executor when engine is garbage collected
        # Use getattr since __init__ can fail before the field is set
        if model_executor := getattr(self, "model_executor", None):
            model_executor.shutdown()

    MISSING_TOKENIZER_GROUP_MSG = ("Unable to get tokenizer because "
                                   "skip_tokenizer_init is True")
    def get_tokenizer_group(
            self,
            fail_msg: str = MISSING_TOKENIZER_GROUP_MSG) -> BaseTokenizerGroup:
        if self.tokenizer is None:
            raise ValueError(fail_msg)
    
    def get_tokenizer(self) -> "PreTrainedTokenizer":
        return self.get_tokenizer_group().get_lora_tokenizer(None)
    def get_tokenizer_for_seq(self,
                              sequence: Sequence) -> "PreTrainedTokenizer":
        return self.tokenizer.get_lora_tokenizer(sequence.lora_request)

    def _init_tokenizer(self, **tokenizer_init_kwargs) -> BaseTokenizerGroup:
        init_kwargs = dict(
            tokenizer_id=self.model_config.tokenizer,
            enable_lora=bool(self.lora_config),
            max_num_seqs=2048,
            max_input_length=None,
            tokenizer_mode=self.model_config.tokenizer_mode,
            trust_remote_code=self.model_config.trust_remote_code,
            revision=self.model_config.tokenizer_revision)
        init_kwargs.update(tokenizer_init_kwargs)

        return get_tokenizer_group(self.parallel_config.tokenizer_pool_config,
                                   **init_kwargs)

    def _verify_args(self) -> None:
        self.model_config.verify_with_parallel_config(self.parallel_config)
        self.cache_config.verify_with_parallel_config(self.parallel_config)
        if self.lora_config:
            self.lora_config.verify_with_model_config(self.model_config)
            self.lora_config.verify_with_scheduler_config(
                self.scheduler_config)
        if self.prompt_adapter_config:
            self.prompt_adapter_config.verify_with_model_config(
                self.model_config)
    def encode_request(
        self,
        request_id: str,  # pylint: disable=unused-argument
        prompt: Optional[str],
        prompt_token_ids: Optional[List[int]] = None,
        lora_request: Optional[LoRARequest] = None,
    ):
        if prompt_token_ids is None:
            assert prompt is not None
            prompt_token_ids = self.tokenizer.encode(request_id=request_id,
                                                     prompt=prompt,
                                                     lora_request=lora_request)
            
        return prompt_token_ids


    def obtain_aux_scores(self, seq_groups:List[SequenceGroup], virtual_engine: int=0):
        
        import time
        t0 = time.time()
        
        aux_model_scores = {}

        for seq_group_original in seq_groups: 
            prompt = seq_group_original.seqs_dict[next(iter(seq_group_original.seqs_dict))].prompt
            sampling_params=SamplingParams(
                n=1,
                temperature=1.0,
                top_p=1.0,
                use_beam_search=False,
                ignore_eos=True,
                max_tokens=1,
            )

            max_logprobs = self.get_model_config().max_logprobs
            if (sampling_params.logprobs
                    and sampling_params.logprobs > max_logprobs) or (
                        sampling_params.prompt_logprobs
                        and sampling_params.prompt_logprobs > max_logprobs):
                raise ValueError(f"Cannot request more than "
                                f"{max_logprobs} logprobs.")
            
            arrival_time = time.time()
            prompt_token_ids = None 
            lora_request = None
            multi_modal_data = None
            prompt_token_ids = self.encode_request(
                request_id=seq_group_original.request_id,
                prompt=prompt,
                prompt_token_ids=prompt_token_ids,
                lora_request=lora_request)[:self.scheduler[virtual_engine].scheduler_config.max_model_len]
            #chunk to max_model_len
            llm_inputs = LLMInputs(prompt_token_ids=prompt_token_ids,
                               prompt=prompt)
        
            # Create the sequences.
            block_size = self.cache_config.block_size
            seq_id = next(self.seq_counter)
            eos_token_id = self.tokenizer.get_lora_tokenizer(
                lora_request).eos_token_id
            seq = Sequence(seq_id, llm_inputs, block_size,
                        eos_token_id, lora_request)

            # Defensive copy of SamplingParams, which are used by the sampler,
            # this doesn't deep-copy LogitsProcessor objects
            sampling_params = sampling_params.clone()
            # inject the eos token id into the sampling_params to support min_tokens
            # processing
            sampling_params.eos_token_id = seq.eos_token_id
            sampling_params.update_from_generation_config(
                self.generation_config_fields) #-> lead to bug when I set ignore_eos=True

            # Create the sequence group.
            seq_group = SequenceGroup(seq_group_original.request_id, [seq], arrival_time, 0, sampling_params,
                                     lora_request, multi_modal_data)

            # Add the sequence group to the scheduler.
            self.scheduler[virtual_engine].add_seq_group(seq_group)
        while self.has_unfinished_requests(virtual_engine):
            step_outputs = self.step(virtual_engine)
            for output in step_outputs:
                if output.finished:
                    aux_model_scores[output.request_id] = output.outputs[0].aux_model_score
        # print(aux_model_scores)
        for seq_group_original in seq_groups: 
            assert seq_group_original.need_aux_model_score()
            seq_group_original.set_aux_model_score(aux_model_scores[seq_group_original.request_id])
    

    
    def add_request(
        self,
        request_id: str,
        prompt: Optional[str],
        sampling_params: SamplingParams,
        prompt_token_ids: Optional[List[int]] = None,
        arrival_time: Optional[float] = None,
        lora_request: Optional[LoRARequest] = None,
        virtual_engine: int=0,
    ) -> None:
        """Add a request to the engine's request pool.

        The request is added to the request pool and will be processed by the
        scheduler as `engine.step()` is called. The exact scheduling policy is
        determined by the scheduler.

        Args:
            request_id: The unique ID of the request.
            prompt: The prompt string. Can be None if prompt_token_ids is
                provided.
            sampling_params: The sampling parameters for text generation.
            prompt_token_ids: The token IDs of the prompt. If None, we
                use the tokenizer to convert the prompts to token IDs.
            arrival_time: The arrival time of the request. If None, we use
                the current monotonic time.
            multi_modal_data: Multi modal data per request.

        Details:
            - Set arrival_time to the current time if it is None.
            - Set prompt_token_ids to the encoded prompt if it is None.
            - Create `best_of` number of :class:`~vllm.Sequence` objects.
            - Create a :class:`~vllm.SequenceGroup` object
              from the list of :class:`~vllm.Sequence`.
            - Add the :class:`~vllm.SequenceGroup` object to the scheduler.

        Example:
            >>> # initialize engine
            >>> engine = LLMEngine.from_engine_args(engine_args)
            >>> # set request arguments
            >>> example_prompt = "Who is the president of the United States?"
            >>> sampling_params = SamplingParams(temperature=0.0)
            >>> request_id = 0
            >>>
            >>> # add the request to the engine
            >>> engine.add_request(
            >>>    str(request_id),
            >>>    example_prompt,
            >>>    SamplingParams(temperature=0.0))
            >>> # continue the request processing
            >>> ...
        """
        max_logprobs = self.get_model_config().max_logprobs
        if (sampling_params.logprobs
                and sampling_params.logprobs > max_logprobs) or (
                    sampling_params.prompt_logprobs
                    and sampling_params.prompt_logprobs > max_logprobs):
            raise ValueError(f"Cannot request more than "
                             f"{max_logprobs} logprobs.")
        if arrival_time is None:
            arrival_time = time.time()
        prompt_token_ids = self.encode_request(
            request_id=request_id,
            prompt=prompt,
            prompt_token_ids=prompt_token_ids,
            lora_request=lora_request)
        llm_inputs = LLMInputs(prompt_token_ids=prompt_token_ids,
                               prompt=prompt)
        # Create the sequences.
        block_size = self.cache_config.block_size
        seq_id = next(self.seq_counter)
        eos_token_id = self.tokenizer.get_lora_tokenizer(
            lora_request).eos_token_id
        seq = Sequence(seq_id, llm_inputs, block_size,
                       eos_token_id, lora_request)

        # Defensive copy of SamplingParams, which are used by the sampler,
        # this doesn't deep-copy LogitsProcessor objects
        sampling_params = sampling_params.clone()
        # inject the eos token id into the sampling_params to support min_tokens
        # processing
        sampling_params.eos_token_id = seq.eos_token_id
        sampling_params.update_from_generation_config(
            self.generation_config_fields) #-> lead to bug when I set ignore_eos=True

        # Create the sequence group.
        seq_group = SequenceGroup(request_id, [seq], arrival_time,0,sampling_params,
                                   lora_request)

        # Add the sequence group to the scheduler.
        self.scheduler[virtual_engine].add_seq_group(seq_group)

    def abort_request(self, request_id: Union[str, Iterable[str]], virtual_engine: int=0) -> None:
        """Aborts a request(s) with the given ID.

        Args:
            request_id: The ID(s) of the request to abort.

        Details:
            - Refer to the
              :meth:`~vllm.core.scheduler.Scheduler.abort_seq_group`
              from class :class:`~vllm.core.scheduler.Scheduler`.

        Example:
            >>> # initialize engine and add a request with request_id
            >>> request_id = str(0)
            >>> # abort the request
            >>> engine.abort_request(request_id)
        """
        self.scheduler[virtual_engine].abort_seq_group(request_id)

    def get_model_config(self) -> ModelConfig:
        """Gets the model configuration."""
        return self.model_config

    def get_num_unfinished_requests(self,virtual_engine) -> int:
        """Gets the number of unfinished requests."""
        return self.scheduler[virtual_engine].get_num_unfinished_seq_groups()

    def has_unfinished_requests(self,virtual_engine) -> bool:
        """Returns True if there are unfinished requests."""
        return self.scheduler[virtual_engine].has_unfinished_seqs()

    def _process_model_outputs(
            self, output: List[SamplerOutput],
            scheduled_seq_groups: List[SequenceGroup],
            ignored_seq_groups: List[SequenceGroup], virtual_engine: int) -> List[RequestOutput]:
        """Apply the model output to the sequences in the scheduled seq groups.

        Returns RequestOutputs that can be returned to the client.
        """

        now = time.time()

        # Organize outputs by [sequence group][step] instead of
        # [step][sequence group].
        output_by_sequence_group = create_output_by_sequence_group(
            output, num_seq_groups=len(scheduled_seq_groups))

        # Update the scheduled sequence groups with the model outputs.
        for scheduled_seq_group, outputs in zip(scheduled_seq_groups,
                                                output_by_sequence_group):
            seq_group:SequenceGroup = scheduled_seq_group.seq_group
            
            assert len(outputs) == 1
            if outputs[0].aux_model_score is not None and seq_group.need_aux_model_score():
                seq_group.set_aux_model_score(outputs[0].aux_model_score)

            seq_group.update_num_computed_tokens(
                scheduled_seq_group.token_chunk_size)
            # If uncomputed tokens > 0, it means prefill is chunked.
            # We don't need to process outputs in that case.
            if seq_group.get_num_uncomputed_tokens() == 0:
                self.output_processor.process_outputs(seq_group, outputs)

        # Free the finished sequence groups.
        self.scheduler[virtual_engine].free_finished_seq_groups()
        additional_info = AdditionalInfo(
            num_running_to_waiting=0,
            num_waiting_to_running=0,
            recomputed_token_nums=0,
            num_preemption_iter=0)
        # Create the outputs.
        request_outputs: List[RequestOutput] = []
        for scheduled_seq_group in scheduled_seq_groups:
            seq_group = scheduled_seq_group.seq_group
            seq_group.maybe_set_first_token_time(now)
            request_output = RequestOutputFactory.create(
                seq_group, additional_info, 0)
            request_outputs.append(request_output)
        for seq_group in ignored_seq_groups:
            request_output = RequestOutputFactory.create(
                seq_group, additional_info, 0)
            request_outputs.append(request_output)
        return request_outputs

    def step(self, virtual_engine: int) -> List[RequestOutput]:
        """Performs one decoding iteration and returns newly generated results.

        .. figure:: https://i.imgur.com/sv2HssD.png
            :alt: Overview of the step function
            :align: center

            Overview of the step function.

        Details:
            - Step 1: Schedules the sequences to be executed in the next
              iteration and the token blocks to be swapped in/out/copy.

                - Depending on the scheduling policy,
                  sequences may be `preempted/reordered`.
                - A Sequence Group (SG) refer to a group of sequences
                  that are generated from the same prompt.

            - Step 2: Calls the distributed executor to execute the model.
            - Step 3: Processes the model output. This mainly includes:

                - Decodes the relevant outputs.
                - Updates the scheduled sequence groups with model outputs
                  based on its `sampling parameters` (`use_beam_search` or not).
                - Frees the finished sequence groups.

            - Finally, it creates and returns the newly generated results.

        Example:
            >>> # Please see the example/ folder for more detailed examples.
            >>>
            >>> # initialize engine and request arguments
            >>> engine = LLMEngine.from_engine_args(engine_args)
            >>> example_inputs = [(0, "What is LLM?",
            >>>    SamplingParams(temperature=0.0))]
            >>>
            >>> # Start the engine with an event loop
            >>> while True:
            >>>     if example_inputs:
            >>>         req_id, prompt, sampling_params = example_inputs.pop(0)
            >>>         engine.add_request(str(req_id), prompt, sampling_params)
            >>>
            >>>     # continue the request processing
            >>>     request_outputs = engine.step()
            >>>     for request_output in request_outputs:
            >>>         if request_output.finished:
            >>>             # return or show the request output
            >>>
            >>>     if not (engine.has_unfinished_requests() or example_inputs):
            >>>         break
        """
        seq_group_metadata_list, scheduler_outputs = self.scheduler[virtual_engine].schedule()
        if not scheduler_outputs.is_empty():
            execute_model_req = ExecuteModelRequest(
                seq_group_metadata_list=seq_group_metadata_list,
                blocks_to_swap_in=scheduler_outputs.blocks_to_swap_in,
                blocks_to_swap_out=scheduler_outputs.blocks_to_swap_out,
                blocks_to_copy=scheduler_outputs.blocks_to_copy,
                num_lookahead_slots=scheduler_outputs.num_lookahead_slots,
                is_aux_model=True,
                is_tp=self.parallel_config.tensor_parallel_size>1,
                use_aux_model=True)
            output = self.model_executor.execute_model(execute_model_req=execute_model_req)
        else:
            output = []

        request_outputs = self._process_model_outputs(
            output, scheduler_outputs.scheduled_seq_groups,
            scheduler_outputs.ignored_seq_groups, virtual_engine)
       
        return request_outputs
    
    def check_health(self) -> None:
        self.model_executor.check_health()

    
    