"""Sequence and its related classes."""
import copy
from datetime import datetime
import enum
import json
import time
import math
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple, Union

import pandas as pd
import torch

from vllm.lora.request import LoRARequest
from vllm.pooling_params import PoolingParams
from vllm.prompt_adapter.request import PromptAdapterRequest
from vllm.sampling_params import SamplingParams

if TYPE_CHECKING:
    from vllm.inputs import LLMInputs
    from vllm.multimodal import MultiModalDataDict
    from vllm.spec_decode.metrics import SpecDecodeWorkerMetrics


@dataclass
class Logprob:
    """Infos for supporting OpenAI compatible logprobs and token ranks.

    Attributes:
        logprob: The logprob of chosen token
        rank: The vocab rank of chosen token (>=1)
        decoded_token: The decoded chosen token index
    """
    logprob: float
    rank: Optional[int] = None
    decoded_token: Optional[str] = None


# {token_id -> logprob} per each sequence group. None if the corresponding
# sequence group doesn't require prompt logprob.
PromptLogprobs = List[Optional[Dict[int, Logprob]]]
# {token_id -> logprob} for each sequence group.
SampleLogprobs = List[Dict[int, Logprob]]


class SequenceStatus(enum.IntEnum):
    """Status of a sequence."""
    WAITING = 0
    RUNNING = 1
    SWAPPED = 2
    # Note: anything after SWAPPED (2) will be considered
    # as a finished status.
    FINISHED_STOPPED = 3
    FINISHED_LENGTH_CAPPED = 4
    FINISHED_ABORTED = 5
    FINISHED_IGNORED = 6
    PARTIAL_SWAPPED = 7
    All = 8
    WAITING_TO_RUNNING = 9
    RUNNING_TO_WAITING = 10
    RUNNING_TO_FINISHED_STOPPED = 11


    @staticmethod
    def is_finished(status: "SequenceStatus") -> bool:
        return status > SequenceStatus.SWAPPED

    @staticmethod
    def get_finished_reason(status: "SequenceStatus") -> Union[str, None]:
        if status == SequenceStatus.FINISHED_STOPPED:
            finish_reason = "stop"
        elif status == SequenceStatus.FINISHED_LENGTH_CAPPED:
            finish_reason = "length"
        elif status == SequenceStatus.FINISHED_ABORTED:
            finish_reason = "abort"
        elif status == SequenceStatus.FINISHED_IGNORED:
            # The ignored sequences are the sequences whose prompt lengths
            # are longer than the model's length cap. Therefore, the stop
            # reason should also be "length" as in OpenAI API.
            finish_reason = "length"
        else:
            finish_reason = None
        return finish_reason


class SequenceStage(enum.Enum):
    PREFILL = enum.auto()
    DECODE = enum.auto()

class SequenceType(enum.Enum):
    TEMP = enum.auto()
    NORMAL = enum.auto()


@dataclass
class RequestMetrics:
    """Metrics associated with a request."""
    request_id: int
    arrival_time: float
    last_token_time: float
    last_execute_time: float
    waiting_iter_nums: int
    swap_times: int
    first_scheduled_time: Optional[float] = None
    first_token_time: Optional[float] = None
    time_in_queue: Optional[float] = None
    waiting_times: List[float] = field(default_factory=list)
    finished_time: Optional[float] = None

    def to_serializable_dict(self) -> dict:
        """Convert to dictionary with list fields as strings"""
        metrics_dict = asdict(self)
        
        # Convert list fields to JSON strings
        for field_name, value in metrics_dict.items():
            if isinstance(value, list):
                metrics_dict[field_name] = json.dumps(value, ensure_ascii=False)
        
        return metrics_dict
    @classmethod
    def to_dataframe(cls, metrics_list: List['RequestMetrics']) -> pd.DataFrame:
        """Convert to DataFrame with list fields as strings"""
        return pd.DataFrame([m.to_serializable_dict() for m in metrics_list])


class SequenceData:
    """Data associated with a sequence.

    Args:
        prompt_token_ids: The token IDs of the prompt.
        output_token_ids: The token IDs of the output. Set to an empty list if
            None.

    Attributes:
        prompt_token_ids: The token IDs of the prompt.
        output_token_ids: The token IDs of the output.
        cumulative_logprob: The cumulative log probability of the output.
    """

    def __init__(
        self,
        prompt_token_ids: List[int],
        output_token_ids: Optional[List[int]] = None,
    ) -> None:
        self._prompt_token_ids: List[int] = list(prompt_token_ids)
        self._prompt_token_ids_tuple: Tuple[int, ...] = tuple(prompt_token_ids)
        self._output_token_ids: List[int] = (
            list(output_token_ids) if output_token_ids is not None else [])

        self.cumulative_logprob = 0.0
        # The number of tokens that are computed (that run against the model).
        self._num_computed_tokens = 0
        self._stage: SequenceStage = SequenceStage.PREFILL

        self._update_cached_all_tokens()

    def _update_cached_all_tokens(self):
        self._cached_all_token_ids: List[int] = (self._prompt_token_ids +
                                                 self._output_token_ids)

    @property
    def prompt_token_ids(self) -> Tuple[int, ...]:
        return self._prompt_token_ids_tuple

    @prompt_token_ids.setter
    def prompt_token_ids(self, new_prompt_token_ids) -> None:
        self._prompt_token_ids = list(new_prompt_token_ids)
        self._prompt_token_ids_tuple = tuple(new_prompt_token_ids)
        self._update_cached_all_tokens()

    @property
    def output_token_ids(self) -> Tuple[int, ...]:
        return tuple(self._output_token_ids)

    @output_token_ids.setter
    def output_token_ids(self, new_output_token_ids) -> None:
        self._output_token_ids = list(new_output_token_ids)
        self._update_cached_all_tokens()

    def append_token_id(self, token_id: int, logprob: float) -> None:
        self._output_token_ids.append(token_id)
        self._cached_all_token_ids.append(token_id)
        self.cumulative_logprob += logprob

    def get_len(self) -> int:
        return len(self._output_token_ids) + len(self._prompt_token_ids)

    def get_prompt_len(self) -> int:
        return len(self._prompt_token_ids)

    def get_output_len(self) -> int:
        return len(self._output_token_ids)

    def get_token_ids(self) -> List[int]:
        return self._cached_all_token_ids

    def get_prefix_token_ids(
            self, num_tokens: int
    ) -> Tuple[Tuple[int, ...], Optional[Tuple[int, ...]]]:
        """Get prefix tokens, and make the return value hashable"""
        prompt_length = self.get_prompt_len()
        if num_tokens > prompt_length:
            return (self._prompt_token_ids_tuple,
                    tuple(self._output_token_ids[:num_tokens - prompt_length]))
        else:
            return (self._prompt_token_ids_tuple[:num_tokens], None)

    def get_num_computed_tokens(self) -> int:
        """Return the number of prefill tokens that are already computed."""
        return self._num_computed_tokens

    def update_num_computed_tokens(self, num_new_computed_tokens: int):
        """Update number of tokens computed so far."""
        self._num_computed_tokens += num_new_computed_tokens
        assert self._num_computed_tokens <= self.get_len(), (
            self._num_computed_tokens, self.get_len())
        # If all tokens are computed, it means it is in decoding phase.
        if self.get_num_uncomputed_tokens() == 0:
            self._stage = SequenceStage.DECODE

    def reset_state_for_recompute(self) -> None:
        """Reset the number of computed tokens from this sequence. It is
        supposed to be called when a sequence needs to be started from
        the beginning again (e.g., sequence is preempted).
        """
        self._num_computed_tokens = 0
        self._stage = SequenceStage.PREFILL

    def get_num_uncomputed_tokens(self) -> int:
        """Return the number of prefill tokens that are not computed."""
        # we use `get_len()` which includes prompt_len + output_len instead
        # of prompt_len here. This is because during recompute we need to
        # prefill for both prompt and output.
        return self.get_len() - self.get_num_computed_tokens()

    def get_last_token_id(self) -> int:
        if not self._output_token_ids:
            return self._prompt_token_ids[-1]
        return self._output_token_ids[-1]

    def get_prompt_token_ids(self) -> Tuple[int, ...]:
        return self.prompt_token_ids

    def get_output_token_ids(self) -> Tuple[int, ...]:
        return self.output_token_ids

    @property
    def stage(self) -> SequenceStage:
        return self._stage

    def __repr__(self) -> str:
        return (f"SequenceData("
                f"prompt_token_ids={self._prompt_token_ids}, "
                f"output_token_ids={self._output_token_ids}, "
                f"cumulative_logprob={self.cumulative_logprob})")


class Sequence:
    """Stores the data, status, and block information of a sequence.

    Args:
        seq_id: The ID of the sequence.
        inputs: The inputs of the sequence.
        block_size: The block size of the sequence. Should be the same as the
            block size used by the block manager and cache engine.
        lora_request: LoRA request.
        prompt_adapter_request: Prompt Adapter request.

    """

    def __init__(
            self,
            seq_id: int,
            inputs: "LLMInputs",
            block_size: int,
            eos_token_id: Optional[int] = None,
            lora_request: Optional[LoRARequest] = None,
            prompt_adapter_request: Optional[PromptAdapterRequest] = None
    ) -> None:
        self.seq_id = seq_id
        self.inputs = inputs
        self.block_size = block_size
        self.eos_token_id = eos_token_id
        self.lora_request = lora_request
        self.prompt_adapter_request = prompt_adapter_request

        self.data = SequenceData(self.prompt_token_ids)
        self.output_logprobs: SampleLogprobs = []
        self.output_text = ""
        self.seq_type = SequenceType.NORMAL
        self.status = SequenceStatus.WAITING
        self.stop_reason: Union[int, str, None] = None

        # Used for incremental detokenization
        self.prefix_offset = 0
        self.read_offset = 0
        # Input + output tokens
        self.tokens: Optional[List[str]] = None
        
        self.eos_token_prob: List[float] = []
        self.eos_token_prob_diff: float =  1.0
        self.eos_token_prob_pos: List[int] = []

        # Swapped out block raio
        self.swapped_out_block_nums: int = 0

        self.eos_prob_estimation_window = 15
        self.min_eos_rank = -1
        self.default_eos_token_prob = 0.1

    @property
    def n_blocks(self) -> int:
        return math.ceil(self.get_len() / self.block_size)

    @property
    def prompt(self) -> Optional[str]:
        return self.inputs.get("prompt")

    @property
    def prompt_token_ids(self) -> List[int]:
        return self.inputs["prompt_token_ids"]


    @property
    def multi_modal_data(self) -> "MultiModalDataDict":
        return self.inputs.get("multi_modal_data") or {}

    @property
    def lora_int_id(self) -> int:
        return self.lora_request.lora_int_id if self.lora_request else 0

    @property
    def prompt_adapter_id(self) -> int:
        return self.prompt_adapter_request.prompt_adapter_id \
                        if self.prompt_adapter_request else 0

    def get_output_text_to_return(self, buffer_length: int):
        # We return the full output text if the sequence is finished.
        truncate = buffer_length and not self.is_finished()
        return self.output_text[:-buffer_length] if truncate else (
            self.output_text)
    
    def set_seq_type(self, seq_type: SequenceType) -> None:
        self.seq_type = seq_type

    def get_seq_type(self) -> SequenceType:
        return self.seq_type
    
    def get_eos_token_prob(self) -> List[float]:
        if len(self.eos_token_prob) < self.eos_prob_estimation_window:
            return [self.default_eos_token_prob]
        else:
            # return float(np.mean(self.eos_token_prob))
            return self.eos_token_prob
        
    def get_eos_token_prob_diff(self) -> float:
        if len(self.eos_token_prob) >= self.eos_prob_estimation_window and self.eos_token_prob_diff == 1.0:
            self.eos_token_prob_diff = (
                max(self.eos_token_prob[: self.eos_prob_estimation_window])
                - min(self.eos_token_prob[:self.eos_prob_estimation_window])
            ) / self.eos_prob_estimation_window
        return self.eos_token_prob_diff

    def get_tmp_eos_token_prob(self) -> float:
        if len(self.eos_token_prob) > 0:
            tmp_eos_token_prob_diff= (
               (max(self.eos_token_prob) - min(self.eos_token_prob))/len(self.eos_token_prob)
            )
        else:
            tmp_eos_token_prob_diff = 0.0
        return tmp_eos_token_prob_diff

    def get_eos_token_pos(self) -> List[int]:
        if len(self.eos_token_prob_pos) < self.eos_prob_estimation_window:
            return [-1]
        else:
            return self.eos_token_prob_pos

    def update_min_eos_token_rank(self, eos_token_rank):
        if self.min_eos_rank == -1:
            self.min_eos_rank = eos_token_rank
        else:
            self.min_eos_rank = min(self.min_eos_rank, eos_token_rank)

    def update_swapped_out_block_nums(self, swap_out_block_nums: int):
        self.swapped_out_block_nums += swap_out_block_nums
        self.swapped_out_block_nums = min(self.n_blocks, self.swapped_out_block_nums)

    def reset_swapped_out_block_ratio(self):
        self.swapped_out_block_nums = 0

    def get_swapped_out_block_nums(self) -> int:
        return self.swapped_out_block_nums

    def hash_of_block(self, logical_idx: int) -> int:
        # TODO This can produce incorrect hash when block size > prompt size

        # Compute the number of tokens in the sequence
        # TODO: The current hashing function is O(L^2). We should optimize
        # this in the future.
        num_tokens = self.num_hashed_tokens_of_block(logical_idx)
        hashed_tokens = self.data.get_prefix_token_ids(num_tokens)
        return hash((hashed_tokens, self.lora_int_id))

    def num_hashed_tokens_of_block(self, logical_idx: int):
        return logical_idx * self.block_size + self.block_size

    def reset_state_for_recompute(self):
        """Reset the sequence states for recomputation."""
        self.data.reset_state_for_recompute()

    def append_token_id(
        self,
        token_id: int,
        logprobs: Dict[int, Logprob],
    ) -> None:
        assert token_id in logprobs
        self.output_logprobs.append(logprobs)
        self.data.append_token_id(token_id, logprobs[token_id].logprob)
        if self.eos_token_id in logprobs:
            eos_token_prob = logprobs.get(self.eos_token_id, Logprob(self.default_eos_token_prob)).logprob
            eos_token_prob_pos = int(logprobs.get(self.eos_token_id, Logprob(0, -1)).rank)
            self.eos_token_prob.append(eos_token_prob)
            self.eos_token_prob_pos.append(eos_token_prob_pos)
            self.update_min_eos_token_rank(eos_token_prob_pos)

        else:
            self.eos_token_prob.append(self.default_eos_token_prob)
            self.eos_token_prob_pos.append(-1)

    def get_len(self) -> int:
        return self.data.get_len()

    def get_prompt_len(self) -> int:
        return self.data.get_prompt_len()

    def get_output_len(self) -> int:
        return self.data.get_output_len()

    def get_token_ids(self) -> List[int]:
        return self.data.get_token_ids()

    def get_prompt_token_ids(self) -> Tuple[int, ...]:
        return self.data.get_prompt_token_ids()

    def get_last_token_id(self) -> int:
        return self.data.get_last_token_id()

    def get_output_token_ids(self) -> Tuple[int, ...]:
        return self.data.get_output_token_ids()

    def get_cumulative_logprob(self) -> float:
        return self.data.cumulative_logprob

    def get_beam_search_score(self,
                              length_penalty: float = 1.0,
                              seq_len: Optional[int] = None,
                              eos_token_id: Optional[int] = None) -> float:
        """Calculate the beam search score with length penalty.

        Adapted from

        https://github.com/huggingface/transformers/blob/ccb92be23def445f2afdea94c31286f84b89eb5b/src/transformers/generation/beam_search.py#L938
        """
        if seq_len is None:
            seq_len = self.get_len()
            # NOTE: HF implementation does not count the EOS token
            # towards the length, we align with that here for testing.
            if (eos_token_id is not None
                    and self.get_last_token_id() == eos_token_id):
                seq_len -= 1
        return self.get_cumulative_logprob() / (seq_len**length_penalty)

    def is_finished(self) -> bool:
        return SequenceStatus.is_finished(self.status)

    def fork(self, new_seq_id: int) -> "Sequence":
        new_seq = copy.deepcopy(self)
        new_seq.seq_id = new_seq_id
        return new_seq

    def get_num_new_tokens(self) -> int:
        """Get the number of new tokens to be computed.

        Returns:
            The new number of tokens to be computed. I.e., 1 for decode, or
            the remaining prompt size for prefill.
        """
        if self.data.stage == SequenceStage.DECODE:
            return 1
        return self.data.get_num_uncomputed_tokens()

    def is_prefill(self) -> bool:
        return self.data.stage == SequenceStage.PREFILL

    def __repr__(self) -> str:
        return (f"Sequence(seq_id={self.seq_id}, "
                f"status={self.status.name}, "
                f"num_blocks={self.n_blocks}, ")


@dataclass
class SequenceGroupState:
    """Mutable state tied to a specific sequence group"""

    # torch.Generator used in seeded sampling
    generator: Optional = None  # type: ignore


class SequenceGroup:
    """A group of sequences that are generated from the same prompt.

    Args:
        request_id: The ID of the request.
        seqs: The list of sequences.
        sampling_params: The sampling parameters used to generate the outputs.
        arrival_time: The arrival time of the request.
        lora_request: LoRA request.
        embeddings: The embeddings vectors of the prompt of the sequence group
            for an embedding model.
        pooling_params: The pooling parameters used to generate the pooling
            for an embedding model.
        encoder_seq: Optional, the single encoder sequence. Should be None
                     unless you are working with an encoder/decoder model.
        trace_headers: OpenTelemetry trace headers.
        prompt_adapter_request: Prompt Adapter request.
    """

    def __init__(
        self,
        request_id: str,
        seqs: List[Sequence],
        arrival_time: float,
        execution_budget: int, 
        sampling_params: Optional[SamplingParams] = None,
        lora_request: Optional[LoRARequest] = None,
        embeddings: Optional[List[float]] = None,
        pooling_params: Optional[PoolingParams] = None,
        encoder_seq: Optional[Sequence] = None,
        waiting_iter_base: float = 1,
        vocab_size: int = 32000,
        trace_headers: Optional[Dict[str, str]] = None,
        prompt_adapter_request: Optional[PromptAdapterRequest] = None,
    ) -> None:
        self.request_id = request_id
        self.seqs_dict = {seq.seq_id: seq for seq in seqs}
        self.sampling_params = sampling_params
        self.metrics = RequestMetrics(request_id=request_id,
                                      arrival_time=arrival_time,
                                      last_token_time=arrival_time,
                                      first_scheduled_time=None,
                                      waiting_iter_nums=0,
                                      last_execute_time=time.time(),
                                      first_token_time=None,
                                      time_in_queue=None,
                                      swap_times=0,
                                      waiting_times=[],
                                      finished_time=0,
                                    )
        self.lora_request = lora_request
        self.prompt_logprobs: Optional[PromptLogprobs] = None
        self.state = SequenceGroupState()
        self.embeddings = embeddings
        self.pooling_params = pooling_params
        self.prompt_adapter_request = prompt_adapter_request
        self.encoder_seq = encoder_seq
        self.trace_headers = trace_headers
        self.eos_token_id = self.seqs_dict[next(iter(self.seqs_dict))].eos_token_id
        self.current_priority = None
        self.promoted = 0
        self.priority_rate = 1.0
        if self.sampling_params:
            self.max_length = self.sampling_params.max_tokens
        else:
            self.max_length = 10240
        self.tmp_priority_rate=1.0
        self.weighted: Tuple[float, float] = (0, 0)
        self.swap_out_moment = None
        self.swap_in_moment = None
        self.vocab_size = vocab_size
        self.token_chunk_size = 0
        self.type = ""
        self.pred_score = None
        self.pscore = None
        self.aux_model_score = None
        self.lst_process_time = arrival_time
        self.running_info = {"swap_out" : 0, "swap_blocks" : 0}
        self.process_time = 0
        

    @property
    def prompt(self) -> Optional[str]:
        # All sequences in the group should have the same prompt.
        # We use the prompt of an arbitrary sequence.
        return next(iter(self.seqs_dict.values())).prompt

    @property
    def prompt_token_ids(self) -> List[int]:
        # All sequences in the group should have the same prompt.
        # We use the prompt of an arbitrary sequence.
        return next(iter(self.seqs_dict.values())).prompt_token_ids

    @property
    def multi_modal_data(self) -> "MultiModalDataDict":
        # All sequences in the group should have the same multi-modal data.
        # We use the multi-modal data of an arbitrary sequence.
        return next(iter(self.seqs_dict.values())).multi_modal_data

    @property
    def lora_int_id(self) -> int:
        return self.lora_request.lora_int_id if self.lora_request else 0

    @property
    def prompt_adapter_id(self) -> int:
        return self.prompt_adapter_request.prompt_adapter_id \
                        if self.prompt_adapter_request else 0

    @property
    def prompt_adapter_num_virtual_tokens(self) -> int:
        return self.prompt_adapter_request.prompt_adapter_num_virtual_tokens\
                         if self.prompt_adapter_request else 0

    def need_aux_model_score(self) -> bool:
        return self.aux_model_score is None 
    
    def set_aux_model_score(self, aux_model_score) -> None:
        self.aux_model_score = aux_model_score

    def set_pred_score(self, pred_score):
        self.pred_score = pred_score


    def get_last_latency(self, now: float) -> Optional[float]:
        """Sets the last token time for Request level timings."""
        # If still in prefill phase, raise Error.
        if self.is_prefill():
            raise ValueError(
                "seq_group.get_last_latency() should not be called "
                "if the seq_group is in prefill phase.")

        # Otherwise return token latency.
        latency = now - self.metrics.last_token_time
        self.metrics.last_token_time = now
        return latency

    def maybe_set_first_token_time(self, time: float) -> None:
        """Sets the first token time for Request level timings."""
        # Note: in a case where a sequence_group is swapped and
        #   recomputed, the time between iterations is counted
        #   in TPOT, rather than recalculating TTFT (since from the )
        #   POV of the user, there is simply a long generation delay.
        if (self.metrics.first_token_time is None
                and self.get_seqs()[0].get_output_len() == 1):
            self.metrics.first_token_time = time

    def maybe_set_first_scheduled_time(self, time: float) -> None:
        """Sets the first scheduled time and time in queue for Request
        level timings."""
        if self.metrics.first_scheduled_time is None:
            self.metrics.first_scheduled_time = time
            self.metrics.time_in_queue = time - self.metrics.arrival_time

    def set_finished_time(self, time: Optional[float]) -> None:
        """Sets the finished time for Request level timings."""
        self.metrics.finished_time = time

    def update_last_execute_time(self):
        self.metrics.waiting_times.append(time.time() - self.metrics.last_execute_time)
        self.metrics.last_execute_time = time.time()

    def get_last_execute_time(self):
        return self.metrics.last_execute_time

    def update_waiting_iter_nums(self):
        self.metrics.waiting_iter_nums += 1
    
    def update_swap_times(self):
        self.metrics.swap_times += 1

    def reset_waiting_iter_nums(self):
        self.metrics.waiting_iter_nums = 0

    def get_max_num_running_seqs(self) -> int:
        """The maximum number of sequences running in parallel in the remaining
        lifetime of the request."""
        if self.sampling_params and self.sampling_params.use_beam_search:
            # For beam search, maximally there will always be `best_of` beam
            # candidates running in the future.
            return self.sampling_params.best_of
        else:
            if (self.sampling_params
                    and self.sampling_params.best_of > self.num_seqs()):
                # At prompt stage, the sequence group is not yet filled up
                # and only have one sequence running. However, in the
                # generation stage, we will have `best_of` sequences running.
                return self.sampling_params.best_of
            # At sampling stages, return the number of actual sequences
            # that are not finished yet.
            return self.num_unfinished_seqs()

    def get_seqs(
        self,
        status: Optional[SequenceStatus] = None,
    ) -> List[Sequence]:
        return list(self.seqs_dict.values()) if status is None else [
            seq for seq in self.seqs_dict.values() if seq.status == status
        ]

    def is_encoder_decoder(self) -> bool:
        return self.encoder_seq is not None

    def get_encoder_seq(self) -> Optional[Sequence]:
        return self.encoder_seq

    def get_unfinished_seqs(self) -> List[Sequence]:
        return [
            seq for seq in self.seqs_dict.values() if not seq.is_finished()
        ]

    def get_finished_seqs(self) -> List[Sequence]:
        return [seq for seq in self.seqs_dict.values() if seq.is_finished()]

    def update_num_computed_tokens(self, num_new_computed_tokens: int):
        """Update number of tokens computed so far."""
        for seq in self.seqs_dict.values():
            if not seq.is_finished():
                seq.data.update_num_computed_tokens(num_new_computed_tokens)

    def get_num_uncomputed_tokens(self) -> int:
        num_uncomputed_tokens = 0
        for seq in self.get_seqs():
            if not seq.is_finished():
                num_uncomputed_tokens += seq.data.get_num_uncomputed_tokens()
        return num_uncomputed_tokens

    @property
    def seq_len(self) -> int:
        return sum([seq.get_len() for seq in self.seqs_dict.values()])
    
    @property
    def decode_len(self) -> int:
        return sum([seq.get_output_len() for seq in self.seqs_dict.values()])
    
    @property
    def total_token_block_size(self) -> int:
        _total_token_block_size = sum([seq.n_blocks for seq in self.seqs_dict.values()])
        return _total_token_block_size
        # if self.is_prefill():
        #     return _total_token_block_size + 1
        # else:
        #     return _total_token_block_size + len(self.get_seqs())
    
    def num_seqs(self, status: Optional[SequenceStatus] = None) -> int:
        # Optimization. We don't need to call get_seqs if we don't need to
        # filter by states.
        if status is None:
            return len(self.seqs_dict)

        return len(self.get_seqs(status))

    def num_unfinished_seqs(self) -> int:
        return len(self.get_unfinished_seqs())

    def num_finished_seqs(self) -> int:
        return len(self.get_finished_seqs())

    def find(self, seq_id: int) -> Sequence:
        if seq_id not in self.seqs_dict:
            raise ValueError(f"Sequence {seq_id} not found.")
        return self.seqs_dict[seq_id]

    def add(self, seq: Sequence) -> None:
        if seq.seq_id in self.seqs_dict:
            raise ValueError(f"Sequence {seq.seq_id} already exists.")
        self.seqs_dict[seq.seq_id] = seq

    def remove(self, seq_id: int) -> None:
        if seq_id not in self.seqs_dict:
            raise ValueError(f"Sequence {seq_id} not found.")
        del self.seqs_dict[seq_id]

    def is_finished(self) -> bool:
        return all(seq.is_finished() for seq in self.get_seqs())

    def is_prefill(self) -> bool:
        # Every sequence should be in the same stage.
        return self.get_seqs()[0].is_prefill()

    def __repr__(self) -> str:
        return (f"SequenceGroup(request_id={self.request_id}, "
                f"sampling_params={self.sampling_params}, "
                f"num_seqs={len(self.seqs_dict)})")


class SequenceGroupMetadata:
    """Metadata for a sequence group. Used to create `AttentionMetadata`.

    Args:
        request_id: The ID of the request.
        is_prompt: Whether the request is at prompt stage.
        seq_data: The sequence data. (Seq id -> sequence data)
        sampling_params: The sampling parameters used to generate the outputs.
        block_tables: The block tables. (Seq id -> list of physical block
            numbers)
        do_sample: True if sampling is required. Sampling is not required when
            e.g., prefill is chunked, and the current iteration only computes
            query tokens for prefill, we don't need sampling.
        token_chunk_size: The number of tokens to be processed (per sequence).
            None if chunking is not required.
        lora_request: LoRA request.
        computed_block_nums: The block numbers that are already computed,
            used in prefix caching.
        state: Internal state tied to this sequence group.
        multi_modal_data: Multi modal data.
        encoder_seq_data: Optional sequence data for encoder prompt
                          (SequenceGroup.encoder_seq). Should be None 
                          unless you are working with an encoder/decoder
                          model.
        cross_block_table: Optional cross-attention block table associated
                           with the encoder prompt
                           (SequenceGroup.encoder_seq). Should be None
                           unless you are working with an encoder/decoder
                           model.
        prompt_adapter_request: Prompt Adapter request.
    """

    def __init__(
        self,
        request_id: str,
        is_prompt: bool,
        seq_data: Dict[int, SequenceData],
        sampling_params: SamplingParams,
        block_tables: Dict[int, List[int]],
        do_sample: bool = True,
        pooling_params: Optional[PoolingParams] = None,
        token_chunk_size: Optional[int] = None,
        lora_request: Optional[LoRARequest] = None,
        computed_block_nums: Optional[List[int]] = None,
        state: Optional[SequenceGroupState] = None,
        multi_modal_data: Optional["MultiModalDataDict"] = None,
        encoder_seq_data: Optional[SequenceData] = None,
        cross_block_table: Optional[List[int]] = None,
        prompt_adapter_request: Optional[PromptAdapterRequest] = None,
        eos_token_id: Optional[int] = None,
    ) -> None:
        self.request_id = request_id
        self.is_prompt = is_prompt
        self.seq_data = seq_data
        self.sampling_params = sampling_params
        self.block_tables = block_tables
        self.pooling_params = pooling_params
        self.lora_request = lora_request
        self.prompt_adapter_request = prompt_adapter_request
        self.computed_block_nums = computed_block_nums
        self.multi_modal_data = multi_modal_data
        self.state = SequenceGroupState() if state is None else state
        self.encoder_seq_data = encoder_seq_data
        self.cross_block_table = cross_block_table
        self._token_chunk_size = token_chunk_size
        self.do_sample = do_sample

        # The number of speculative tokens adopted in this request.
        # None means specuative decoding is not used.
        # Zero means speculative decoding is disabled for some reasons.
        # TODO: We should maintain this states out of the sequence group.
        self.num_speculative_tokens = None
        self.eos_token_id = eos_token_id
        if self._token_chunk_size is None:
            if is_prompt:
                self._token_chunk_size = list(seq_data.values())[0].get_len()
            else:
                self._token_chunk_size = 1

    @property
    def lora_int_id(self) -> int:
        return self.lora_request.lora_int_id if self.lora_request else 0

    @property
    def prompt_adapter_id(self) -> int:
        return self.prompt_adapter_request.prompt_adapter_id \
                        if self.prompt_adapter_request else 0

    @property
    def prompt_adapter_num_virtual_tokens(self) -> int:
        return self.prompt_adapter_request.prompt_adapter_num_virtual_tokens \
                        if self.prompt_adapter_request else 0

    @property
    def token_chunk_size(self) -> int:
        """Return the number of tokens to be processed (chunk size)."""
        assert self._token_chunk_size is not None
        return self._token_chunk_size


class SequenceOutput:
    """The model output associated with a sequence.

    Args:
        parent_seq_id: The ID of the parent sequence (for forking in beam
            search).
        output_token: The output token ID.
        logprobs: The logprobs of the output token.
            (Token id -> logP(x_i+1 | x_0, ..., x_i))
    """

    def __init__(
        self,
        parent_seq_id: int,
        output_token: int,
        logprobs: Dict[int, Logprob],
    ) -> None:
        self.parent_seq_id = parent_seq_id
        self.output_token = output_token
        self.logprobs = logprobs

    def __repr__(self) -> str:
        return (f"SequenceOutput(parent_seq_id={self.parent_seq_id}, "
                f"output_token={self.output_token}, "
                f"logprobs={self.logprobs})")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SequenceOutput):
            raise NotImplementedError()
        equal = (self.parent_seq_id == other.parent_seq_id
                 and self.output_token == other.output_token)
        log_probs_equal = other.logprobs == self.logprobs
        return equal and log_probs_equal


class SequenceGroupOutput(ABC):
    """The base class for model outputs associated with a sequence group."""

    @abstractmethod
    def __repr__(self) -> str:
        pass

    @abstractmethod
    def __eq__(self, other: object) -> bool:
        pass


class CompletionSequenceGroupOutput(SequenceGroupOutput):
    """The model output associated with a completion sequence group."""

    def __init__(
        self,
        samples: List[SequenceOutput],
        prompt_logprobs: Optional[PromptLogprobs],
        pred_score: Optional[float] = None,
        aux_model_score: Optional[float] = None,
    ) -> None:
        self.samples = samples
        # Prompt logprob for each prompt query token.
        self.prompt_logprobs = prompt_logprobs
        self.pred_score = pred_score
        self.aux_model_score = aux_model_score

    def __repr__(self) -> str:
        return (f"CompletionSequenceGroupOutput(samples={self.samples}, "
                f"prompt_logprobs={self.prompt_logprobs}), "
                f"aux_model_score={self.aux_model_score}, "
                f"pred_score={self.pred_score})")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CompletionSequenceGroupOutput):
            raise NotImplementedError()
        return (self.samples == other.samples
                and self.prompt_logprobs == other.prompt_logprobs 
                   and self.pred_score == other.pred_score
                      and self.aux_model_score == other.aux_model_score)


class EmbeddingSequenceGroupOutput(SequenceGroupOutput):
    """The model output associated with an embedding sequence group."""

    def __init__(
        self,
        embeddings: List[float],
    ) -> None:
        self.embeddings = embeddings

    def __repr__(self) -> str:
        return (f"EmbeddingSequenceGroupOutput("
                f"embeddings_shape={len(self.embeddings)})")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, EmbeddingSequenceGroupOutput):
            raise NotImplementedError()
        return self.embeddings == other.embeddings


@dataclass
class IntermediateTensors:
    """For all pipeline stages except the last, we need to return the hidden
    states and residuals to be sent to the next stage. This data structure
    contains the hidden states and residuals for a request.
    """

    tensors: Dict[str, torch.Tensor]

    def __getitem__(self, key: Union[str, slice]):
        if isinstance(key, str):
            return self.tensors[key]
        elif isinstance(key, slice):
            return self.__class__({k: v[key] for k, v in self.tensors.items()})

    def __setitem__(self, key: str, value):
        self.tensors[key] = value

    def __len__(self):
        return len(self.tensors)

    def __eq__(self, other: object):
        return isinstance(other, self.__class__) and self

    def __repr__(self) -> str:
        return f"IntermediateTensors(tensors={self.tensors})"


@dataclass
class SamplerOutput:
    """For each sequence group, we generate a list of SequenceOutput object,
    each of which contains one possible candidate for the next token.

    This data structure implements methods, so it can be used like a list, but
    also has optional fields for device tensors.
    """

    outputs: List[CompletionSequenceGroupOutput]

    # On-device tensor containing probabilities of each token.
    sampled_token_probs: Optional[torch.Tensor] = None

    # On-device tensor containing the logprobs of each token.
    logprobs: Optional["torch.Tensor"] = None

    # On-device tensor containing the sampled token ids.
    sampled_token_ids: Optional[torch.Tensor] = None

    swap_time: Optional[float] = 0.0
    # Spec decode metrics populated by workers.
    spec_decode_worker_metrics: Optional["SpecDecodeWorkerMetrics"] = None

    # Optional last hidden states from the model.
    hidden_states: Optional[torch.Tensor] = None

    def __getitem__(self, idx: int):
        return self.outputs[idx]

    def __setitem__(self, idx: int, value):
        self.outputs[idx] = value

    def __len__(self):
        return len(self.outputs)

    def __eq__(self, other: object):
        return isinstance(other,
                          self.__class__) and self.outputs == other.outputs

    def __repr__(self) -> str:
        """Show the shape of a tensor instead of its values to reduce noise.
        """
        sampled_token_probs_repr = ("None" if self.sampled_token_probs is None
                                    else self.sampled_token_probs.shape)
        sampled_token_ids_repr = ("None" if self.sampled_token_ids is None else
                                  self.sampled_token_ids.shape)
        return (
            f"SamplerOutput(outputs={self.outputs}, "
            f"sampled_token_probs={sampled_token_probs_repr}, "
            f"sampled_token_ids={sampled_token_ids_repr}, "
            f"spec_decode_worker_metrics={self.spec_decode_worker_metrics})")


@dataclass
class PoolerOutput:
    """The output from a pooling operation in the embedding model."""
    outputs: List[EmbeddingSequenceGroupOutput]

    spec_decode_worker_metrics: Optional["SpecDecodeWorkerMetrics"] = None

    def __getitem__(self, idx: int):
        return self.outputs[idx]

    def __setitem__(self, idx: int, value):
        self.outputs[idx] = value

    def __len__(self):
        return len(self.outputs)

    def __eq__(self, other: object):
        return isinstance(other,
                          self.__class__) and self.outputs == other.outputs


def get_all_seq_ids(
        seq_group_metadata_list: List[SequenceGroupMetadata]) -> List[int]:
    """Given a list of SequenceGroupMetadata, create a list of all
    sequence ids.
    """
    return [seq_id for sg in seq_group_metadata_list for seq_id in sg.seq_data]


def get_all_seq_ids_and_request_ids(
    seq_group_metadata_list: List[SequenceGroupMetadata]
) -> Tuple[List[int], Dict[str, Set[int]]]:
    """Given a list of SequenceGroupMetadata, create a list of all
    sequence ids.
    """
    seq_ids: List[int] = []
    request_id_seq_ids_mapping: Dict[str, Set[int]] = defaultdict(set)
    for sg in seq_group_metadata_list:
        for seq_id in sg.seq_data:
            seq_ids.append(seq_id)
            request_id_seq_ids_mapping[sg.request_id].add(seq_id)
    return seq_ids, request_id_seq_ids_mapping


class HiddenStates:
    """Hidden states corresponding to in-progress sequences.
    Used in speculative decoding to pass hidden states from
    the target model to the proposer model in the subsequent step.

    seq_ids are the sequence ids of each entry of the batch
    dimension of the hidden_states tensor"""

    def __init__(self, seq_group_metadata_list: List[SequenceGroupMetadata],
                 hidden_states: torch.Tensor):
        assert len(seq_group_metadata_list) == len(hidden_states)
        self.seq_ids: List[int] = get_all_seq_ids(seq_group_metadata_list)
        self.hidden_states: torch.Tensor = hidden_states

    def update(self, seq_group_metadata_list: List[SequenceGroupMetadata],
               hidden_states: torch.Tensor) -> None:
        """Update hidden states from target model invocation."""
        assert len(seq_group_metadata_list) == len(hidden_states)
        self.seq_ids.extend(get_all_seq_ids(seq_group_metadata_list))
        self.hidden_states = torch.cat([self.hidden_states, hidden_states])

    def prune(self,
              seq_group_metadata_list: List[SequenceGroupMetadata]) -> None:
        """Prune to provided list of sequence ids."""
        seq_ids = get_all_seq_ids(seq_group_metadata_list)
        if seq_ids != self.seq_ids:
            # Batch contents changed - prune removed sequences.
            index = [self.seq_ids.index(seq_id) for seq_id in seq_ids]
            self.hidden_states = self.hidden_states[index]
            self.seq_ids = seq_ids


@dataclass
class ExecuteModelRequest:
    """The model execution request, containing CPU metadata only. The LLM
    engine should create an instance of this class for each request batch."""
    # The sequence group metadata list.
    seq_group_metadata_list: List[SequenceGroupMetadata]
    # Blocks to swap in. List of CPU -> GPU block number.
    blocks_to_swap_in: List[Tuple[int, int]] = field(default_factory=list)
    # Blocks to swap out. List of GPU -> CPU block number.
    blocks_to_swap_out: List[Tuple[int, int]] = field(default_factory=list)
    # Blocks to copy. Source to dest block.
    blocks_to_copy: List[Tuple[int, int]] = field(default_factory=list)
    # Virtual engine ID for pipeline parallel.
    virtual_engine: int = 0
    # The number of slots for lookahead decoding.
    num_lookahead_slots: int = 0
    # The number of requests in the running queue.
    running_queue_size: int = 0
    # Optional hidden states from prior step.
    previous_hidden_states: Optional[HiddenStates] = None
    # The number of forward steps to run.
    num_steps: int = 1
    # Finished request ids since last step.
    finished_requests_ids: List[str] = field(default_factory=list)
    is_aux_model: bool = False
    use_aux_model: bool = False
    is_tp: bool=False

    def clone(
        self, seq_group_metadata_list: List[SequenceGroupMetadata]
    ) -> "ExecuteModelRequest":
        """Clone the request with a new sequence group metadata list."""
        return ExecuteModelRequest(
            seq_group_metadata_list=seq_group_metadata_list,
            blocks_to_swap_in=self.blocks_to_swap_in.copy(),
            blocks_to_swap_out=self.blocks_to_swap_out.copy(),
            blocks_to_copy=self.blocks_to_copy.copy(),
            virtual_engine=self.virtual_engine,
            num_lookahead_slots=self.num_lookahead_slots,
            running_queue_size=self.running_queue_size,
            previous_hidden_states=self.previous_hidden_states,
            num_steps=self.num_steps,
            finished_requests_ids=self.finished_requests_ids)
