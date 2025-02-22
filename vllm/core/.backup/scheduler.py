import enum
from math import ceil
import numpy as np
import os
import random
from itertools import accumulate
import bisect
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Iterable, List, Optional, Set, Tuple, Union

from vllm.config import CacheConfig, LoRAConfig, SchedulerConfig
from vllm.core.interfaces import AllocStatus, BlockSpaceManager
from vllm.core.policy import Policy, PolicyFactory
from vllm.logger import init_logger
from vllm.lora.request import LoRARequest
from vllm.sequence import (Sequence, SequenceData, SequenceGroup,
                           SequenceGroupMetadata, SequenceStatus)

logger = init_logger(__name__)

# Test-only. If configured, decode is preempted with
# ARTIFICIAL_PREEMPTION_PROB% probability.
ENABLE_ARTIFICIAL_PREEMPT = bool(
    os.getenv("VLLM_TEST_ENABLE_ARTIFICIAL_PREEMPT", False))  # noqa
ARTIFICIAL_PREEMPTION_PROB = 0.5
ARTIFICIAL_PREEMPTION_MAX_CNT = 500


class PreemptionMode(enum.Enum):
    """Preemption modes.

    1. Swapping: Swap out the blocks of the preempted sequences to CPU memory
    and swap them back in when the sequences are resumed.
    2. Recomputation: Discard the blocks of the preempted sequences and
    recompute them when the sequences are resumed, treating the sequences as
    new prompts.
    """
    SWAP = enum.auto()
    RECOMPUTE = enum.auto()


class SwapMode(enum.Enum):
    """Swap modes.

    1. Swap out the blocks of the preempted sequences to CPU memory
    and swap them back in when the sequences are resumed.
    2. Discard the blocks of the preempted sequences and
    recompute them when the sequences are resumed, treating the sequences as
    new prompts.

    """
    FULL = enum.auto()
    PARTIAL = enum.auto()


class PreemptionReason(enum.Enum):
    """ Preemption reasons.
    1. Exhausted token budget
    2. Exhausted sequence budget
    3. All preempted sequences are exhausted
    4. No preempted sequences
    
    """
    BUDGET_EXHAUSTED = enum.auto()
    SEQ_NUM_EXHAUSTED = enum.auto()
    ALL_EXHAUSTED = enum.auto()
    NONE = enum.auto()


@dataclass
class SchedulingBudget:
    """The available slots for scheduling.

    TODO(sang): Right now, the budget is request_id-aware meaning it can ignore
    budget update from the same request_id. It is because in normal scheduling
    path, we update RUNNING num_seqs ahead of time, meaning it could be
    updated more than once when scheduling RUNNING requests. Since this won't
    happen if we only have chunked prefill scheduling, we can remove this
    feature from the API when chunked prefill is enabled by default.
    """
    token_budget: int
    max_num_seqs: int
    _requeset_ids_num_batched_tokens: Set[str] = field(default_factory=set)
    _requeset_ids_num_curr_seqs: Set[str] = field(default_factory=set)
    _num_batched_tokens: int = 0
    _num_curr_seqs: int = 0

    def can_schedule_infer(self, *, num_new_tokens: int,
                           num_new_seqs: int) -> PreemptionReason:
        request_tokens = self.num_batched_tokens + num_new_tokens
        request_seqs = self.num_curr_seqs + num_new_seqs
        if request_tokens >= self.token_budget and request_seqs >= self.max_num_seqs:
            return PreemptionReason.ALL_EXHAUSTED
        elif request_tokens >= self.token_budget and request_seqs < self.max_num_seqs:
            return PreemptionReason.BUDGET_EXHAUSTED
        elif request_tokens <= self.token_budget and request_seqs >= self.max_num_seqs:
            return PreemptionReason.SEQ_NUM_EXHAUSTED
        else:
            return PreemptionReason.NONE

    def can_schedule(self, *, num_new_tokens: int, num_new_seqs: int):
        # assert num_new_tokens != 0
        # assert num_new_seqs != 0
        return (self.num_batched_tokens + num_new_tokens <= self.token_budget
                and self.num_curr_seqs + num_new_seqs <= self.max_num_seqs)

    def remaining_token_budget(self):
        return self.token_budget - self.num_batched_tokens

    def add_num_batched_tokens(self, req_id: str, num_batched_tokens: int):
        if req_id in self._requeset_ids_num_batched_tokens:
            return

        self._requeset_ids_num_batched_tokens.add(req_id)
        self._num_batched_tokens += num_batched_tokens

    def subtract_num_batched_tokens(self, req_id: str,
                                    num_batched_tokens: int):
        if req_id in self._requeset_ids_num_batched_tokens:
            self._requeset_ids_num_batched_tokens.remove(req_id)
            self._num_batched_tokens -= num_batched_tokens

    def subtract_num_batched_tokens_partial(self, num_batched_tokens: int):
        self._num_batched_tokens -= num_batched_tokens

    def add_num_seqs(self, req_id: str, num_curr_seqs: int):
        if req_id in self._requeset_ids_num_curr_seqs:
            return

        self._requeset_ids_num_curr_seqs.add(req_id)
        self._num_curr_seqs += num_curr_seqs

    def subtract_num_seqs(self, req_id: str, num_curr_seqs: int):
        if req_id in self._requeset_ids_num_curr_seqs:
            self._requeset_ids_num_curr_seqs.remove(req_id)
            self._num_curr_seqs -= num_curr_seqs

    @property
    def num_batched_tokens(self):
        return self._num_batched_tokens

    @property
    def num_curr_seqs(self):
        return self._num_curr_seqs


@dataclass
class ScheduledSequenceGroup:
    # A sequence group that's scheduled.
    seq_group: SequenceGroup
    # The total chunk size (number of tokens) to process for next iteration.
    # 1 for decoding. Same as prompt tokens for prefill, but if prefill is
    # chunked, it can be smaller than that.
    token_chunk_size: int


@dataclass
class SchedulerOutputs:
    """The scheduling decision made from a scheduler."""
    # Scheduled sequence groups.
    scheduled_seq_groups: Iterable[ScheduledSequenceGroup]
    # Number of prefill groups scheduled.
    num_prefill_groups: int
    # Total number of batched tokens.
    num_batched_tokens: int
    # Blocks to swap in. List of CPU -> GPU block number.
    blocks_to_swap_in: List[Tuple[int, int]]
    # Blocks to swap out. List of GPU -> CPU block number.
    blocks_to_swap_out: List[Tuple[int, int]]
    # Blocks to copy. Source to dest block.
    blocks_to_copy: List[Tuple[int, int]]
    # Sequence groups that are going to be ignored.
    ignored_seq_groups: List[SequenceGroup]
    # The number of slots for lookahead decoding.
    num_lookahead_slots: int
    # The number of requests in the running queue
    running_queue_size: int
    preempted: int
    num_waiting_to_running: int
    num_running_to_waiting: int
    recomputed_token_nums: int

    def __post_init__(self):
        # Swap in and swap out should never happen at the same time.
        # assert not (self.blocks_to_swap_in and self.blocks_to_swap_out)

        self.num_loras: int = len(self.lora_requests)
        if self.num_loras > 0:
            self._sort_by_lora_ids()

    def is_empty(self) -> bool:
        # NOTE: We do not consider the ignored sequence groups.
        return (not self.scheduled_seq_groups and not self.blocks_to_swap_in
                and not self.blocks_to_swap_out and not self.blocks_to_copy)

    def _sort_by_lora_ids(self):
        self.scheduled_seq_groups = sorted(
            self.scheduled_seq_groups,
            key=lambda g: (g.seq_group.lora_int_id, g.seq_group.request_id))

    @property
    def lora_requests(self) -> Set[LoRARequest]:
        return {
            g.seq_group.lora_request
            for g in self.scheduled_seq_groups
            if g.seq_group.lora_request is not None
        }


@dataclass
class SchedulerPreemption:
    decode_seq_groups_running: List[SequenceGroup]
    decode_seq_groups_swapped: List[SequenceGroup]
    prefill_seq_groups_running: List[SequenceGroup]
    prefill_seq_groups_swapped: List[SequenceGroup]
    preempted_running: List[SequenceGroup]
    swapped_out_running: List[SequenceGroup]
    blocks_to_swap_in: List[Tuple[int, int]]
    blocks_to_swap_out: List[Tuple[int, int]]
    blocks_to_copy_running: List[Tuple[int, int]]
    blocks_to_copy_swapped: List[Tuple[int, int]]
    infeasible_seq_groups: List[SequenceGroup]
    ignored_seq_groups: List[SequenceGroup]
    seq_groups_prefill: List[SequenceGroup]
    num_lookahead_slots_running: int
    num_lookahead_slots_swapped: int
    num_lookahead_slots_prefill: int


@dataclass
class SchedulerRunningOutputs:
    """The requests that are scheduled from a running queue.

    Could contain prefill (prefill that's chunked) or decodes. If there's not
    enough memory, it can be preempted (for recompute) or swapped out.
    """
    # Selected sequences that are running and in a decoding phase.
    decode_seq_groups: List[SequenceGroup]
    # Selected sequences that are running and in a prefill phase.
    # I.e., it means the prefill has been chunked.
    prefill_seq_groups: List[SequenceGroup]
    # The preempted sequences.
    preempted: List[SequenceGroup]
    # Sequences that are swapped out.
    swapped_out: List[SequenceGroup]
    # The blocks to swap out.
    blocks_to_swap_out: List[Tuple[int, int]]
    # The blocks to copy.
    blocks_to_copy: List[Tuple[int, int]]
    # The number of slots for lookahead decoding.
    num_lookahead_slots: int

    @classmethod
    def create_empty(cls) -> "SchedulerRunningOutputs":
        return SchedulerRunningOutputs(
            decode_seq_groups=[],
            prefill_seq_groups=[],
            preempted=[],
            swapped_out=[],
            blocks_to_swap_out=[],
            blocks_to_copy=[],
            num_lookahead_slots=0,
        )


@dataclass
class SchedulerSwappedInOutputs:
    """The requests that are scheduled from a swap queue.

    Could contain prefill (prefill that's chunked) or decodes.
    """
    # Selected sequences that are going to be swapped in and is in a
    # decoding phase.
    decode_seq_groups: List[SequenceGroup]
    # Selected sequences that are going to be swapped in and in a prefill
    # phase. I.e., it means the prefill has been chunked.
    prefill_seq_groups: List[SequenceGroup]
    # The blocks to swap in.
    blocks_to_swap_in: List[Tuple[int, int]]
    # The blocks to copy.
    blocks_to_copy: List[Tuple[int, int]]
    # The number of slots for lookahead decoding.
    num_lookahead_slots: int
    # Infeasible sequence groups.
    infeasible_seq_groups: List[SequenceGroup]

    @classmethod
    def create_empty(cls) -> "SchedulerSwappedInOutputs":
        return SchedulerSwappedInOutputs(
            decode_seq_groups=[],
            prefill_seq_groups=[],
            blocks_to_swap_in=[],
            blocks_to_copy=[],
            num_lookahead_slots=0,
            infeasible_seq_groups=[],
        )


@dataclass
class SchedulerPrefillOutputs:
    """The requests that are scheduled from a waiting queue.

    Could contain a fresh prefill requests or preempted requests that need
    to be recomputed from scratch.
    """
    # Selected sequences for prefill.
    seq_groups: List[SequenceGroup]
    # Ignored sequence groups.
    ignored_seq_groups: List[SequenceGroup]
    num_lookahead_slots: int

    @classmethod
    def create_empty(cls) -> "SchedulerPrefillOutputs":
        return SchedulerPrefillOutputs(
            seq_groups=[],
            ignored_seq_groups=[],
            num_lookahead_slots=0,
        )


class Scheduler:

    def __init__(
        self,
        scheduler_config: SchedulerConfig,
        cache_config: CacheConfig,
        lora_config: Optional[LoRAConfig],
    ) -> None:
        self.scheduler_config = scheduler_config
        self.cache_config = cache_config
        # Note for LoRA scheduling: the current policy is extremely
        # simple and NOT fair. It can lead to starvation of some
        # LoRAs. This should be improved in the future.
        self.lora_config = lora_config

        version = "v1"
        if self.scheduler_config.use_v2_block_manager:
            version = "v2"
        if self.scheduler_config.embedding_mode:
            version = "embedding"

        BlockSpaceManagerImpl = BlockSpaceManager.get_block_space_manager_class(
            version)
        self.ddl = None
        self.reach_ddl = False
        # Create the block space manager.
        self.block_manager = BlockSpaceManagerImpl(
            block_size=self.cache_config.block_size,
            num_gpu_blocks=self.cache_config.num_gpu_blocks,
            num_cpu_blocks=self.cache_config.num_cpu_blocks,
            sliding_window=self.cache_config.sliding_window,
            enable_caching=self.cache_config.enable_prefix_caching)

        # Sequence groups in the WAITING state.
        # Contain new prefill or preempted requests.
        self.waiting: Deque[SequenceGroup] = deque()
        # Sequence groups in the RUNNING state.
        # Contain decode requests.
        self.running: Deque[SequenceGroup] = deque()
        # Sequence groups in the SWAPPED state.
        # Contain decode requests that are swapped out.
        self.swapped: Deque[SequenceGroup] = deque()

        # Time at previous scheduling step
        self.prev_time = 0.0
        # Did we schedule a prompt at previous step?
        self.prev_prompt = False
        # Latency of the last prompt step
        self.last_prompt_latency = 0.0
        # preemption mode, RECOMPUTE or SWAP
        self.user_specified_preemption_mode = scheduler_config.preemption_mode

        # The following field is test-only. It is used to inject artificial
        # preemption.
        self.enable_artificial_preemption = ENABLE_ARTIFICIAL_PREEMPT
        self.artificial_preempt_cnt = (ARTIFICIAL_PREEMPTION_MAX_CNT
                                       if self.enable_artificial_preemption
                                       else 0)
        self.num_cumulative_preemption: int = 0
        self.preemption_mode: PreemptionMode = PreemptionMode.RECOMPUTE

        if self.scheduler_config.preemption_mode == "swap":
            self.preemption_mode = PreemptionMode.SWAP
        elif self.scheduler_config.preemption_mode == "recompute":
            self.preemption_mode = PreemptionMode.RECOMPUTE
        self.iter_nums = 0
        # partial swapped dict: key is sequence group, value is a tuple of (remaining block sizes, preempted_seq_group)
        self.partial_swapped: Dict[str, Tuple[int, SequenceGroup]] = {}

        # self.partial_swapped_values:SortedKeyList[(int, SequenceGroup)]=SortedKeyList(key=lambda x: x[0])
        self.partial_swapped_values: List[Tuple[int, str]] = []
        self.seq_group_for_preempted: Tuple[SequenceGroup, int] = ()

        self.total_swap_out_blocks = 0
        self.total_swap_in_blocks = 0
        self.total_swap_out_seqs = 0
        self.total_swap_in_seqs = 0
        self.total_low_eff_swap_out = 0
        self.total_low_eff_swap_out_diff = 0
        self.total_swap_out_waiting_time = 0.0
        self.avg_iter_time = 0.0
        self.avg_block_size = 0.0
        self.has_finished_seqs = False
        self.total_running_block_size = 0
        self.partial_swap_out_flag = self.scheduler_config.swap_out_tokens_policy == "partial"
        self.partial_swapped_rate = self.scheduler_config.swap_out_partial_rate
        self.sort_time_iter = 0.0
        self.swap_while = 0.0
        self.prefill_while = 0.0
        self.schedule_running_time = 0.0
        self.schedule_waiting_time = 0.0
        self.schedule_swapped_time = 0.0
        
        # Motivation:
        self.gpu_memory_iter = 0
        self.gpu_computation_iter = 0

    @property
    def lora_enabled(self) -> bool:
        return bool(self.lora_config)

    @property
    def num_decoding_tokens_per_seq(self) -> int:
        """The number of new tokens."""
        return 1

    def add_seq_group(self, seq_group: SequenceGroup) -> None:
        # Add sequence groups to the waiting queue.
        self.waiting.append(seq_group)

    def abort_seq_group(self, request_id: Union[str, Iterable[str]]) -> None:
        """Aborts a sequence group with the given ID.

        Check if the sequence group with the given ID
            is present in any of the state queue.
        If present, remove the sequence group from the state queue.
            Also, if any of the sequences in the sequence group is not finished,
                free the sequence with status `FINISHED_ABORTED`.
        Otherwise, do nothing.

        Args:
            request_id: The ID(s) of the sequence group to abort.
        """
        if isinstance(request_id, str):
            request_id = (request_id, )
        request_ids = set(request_id)
        for state_queue in [self.waiting, self.running, self.swapped]:
            aborted_groups: List[SequenceGroup] = []
            for seq_group in state_queue:
                if not request_ids:
                    # Using 'break' here may add two extra iterations,
                    # but is acceptable to reduce complexity.
                    break
                if seq_group.request_id in request_ids:
                    # Appending aborted group into pending list.
                    aborted_groups.append(seq_group)
                    request_ids.remove(seq_group.request_id)
            for aborted_group in aborted_groups:
                # Remove the sequence group from the state queue.
                state_queue.remove(aborted_group)
                for seq in aborted_group.get_seqs():
                    if seq.is_finished():
                        continue
                    seq.status = SequenceStatus.FINISHED_ABORTED
                    self.free_seq(seq)

    def has_unfinished_seqs(self) -> bool:
        return len(self.waiting) != 0 or len(self.running) != 0 or len(
            self.swapped) != 0

    def get_num_unfinished_seq_groups(self) -> int:
        return len(self.waiting) + len(self.running) + len(self.swapped)

    def _insert_seq_group_into_partial_swapped(
            self, remaining_block_sizes: int, seq_group_request_id: str,
            seq_group: SequenceGroup) -> None:
        """Insert a sequence group into the partial_swapped queue.
        """
        self.partial_swapped[seq_group_request_id] = (remaining_block_sizes,
                                                      seq_group)
        self.partial_swapped_values.append(
            (remaining_block_sizes, seq_group_request_id))

    def _get_seq_group_from_partial_swapped(
            self, seq_group_reqeust_id: str) -> Tuple[int, SequenceGroup]:
        """Get a sequence group from the partial_swapped queue.
        """
        (left_block_size,
         seq_group) = self.partial_swapped.pop(seq_group_reqeust_id)
        self.partial_swapped_values.remove(
            (left_block_size, seq_group_reqeust_id))
        return (left_block_size, seq_group)
    
    # def _swap_out_partial_v1(
    #         self, seq_group_request_id: str, seq_group: SequenceGroup,
    #         budget: SchedulingBudget,
    #         num_running_tokens: int) -> Tuple[bool, Dict[SequenceGroup, int]]:
    #     """Swap out the sequence group in the partial swapped to CPU.

    #     Args:
    #         seq_group: The sequence group to swap out.
    #         budget: The scheduling budget.
    #         block_size: The size of the block to swap out.
    #     Returns:
    #         A tuple of (is_swap_out, 
    #                     swapped_out_seq_groups, 
    #                     swap_out_block_nums).
    #         is_swap_out: True if the sequence group is swapped out, 
    #                     False otherwise.
    #     """
    #     swapped_out_seq_groups: Dict[SequenceGroup, int] = {}
    #     seq_group_token_num = num_running_tokens
    #     # if len(self.waiting) > 0:
    #     #     # swap out all blocks for the prefill request
    #     #     budget.subtract_num_batched_tokens(seq_group_request_id,
    #     #                                        seq_group_token_num)
    #     #     swapped_out_seq_groups[seq_group] = -1 # swap out the whole seq_group
    #     #     return True, swapped_out_seq_groups
    #     swap_out_rate = self.scheduler_config.swap_out_partial_rate
        
    #     # Swap out a partial block.
    #     seq_group_block_size = seq_group.total_token_block_size
    #     block_unit = max(int(seq_group_block_size * swap_out_rate), 1)
    #     if len(self.partial_swapped) == 0:
    #         # swap out part of the current seq_group for decode request
    #         swap_out_block_num = block_unit
    #         budget.subtract_num_batched_tokens(seq_group_request_id,
    #                                            seq_group_token_num)
    #         r_bs = seq_group_block_size - swap_out_block_num
    #         if r_bs > 0:
    #             self._insert_seq_group_into_partial_swapped(
    #                 r_bs, seq_group_request_id, seq_group)
    #         swapped_out_seq_groups[seq_group] = swap_out_block_num
    #         return True, swapped_out_seq_groups
    #     else:
    #         # swap out left part of the sequence group in the
    #         # partial_swapped queue prior to the current seq_group
    #         partial_swapped_values = self.partial_swapped_values
    #         partial_swapped_values.sort(key=lambda x: x[0])
    #         partial_swapped_bn, partial_swapped_sgs = map(
    #             list, zip(*partial_swapped_values))
    #         selected_swapped_sg_index = self.min_numbers_sum_at_least(
    #             partial_swapped_bn, seq_group_block_size)
    #         if selected_swapped_sg_index == -1:
    #             # swap out part of the current seq_group for decode request due to the lack of free blocks from partial_swapped queue
    #             block_unit = max(int(seq_group_block_size * swap_out_rate), 1)
    #             swap_out_block_num = block_unit
    #             budget.subtract_num_batched_tokens(seq_group_request_id,
    #                                                seq_group_token_num)
    #             r_bs = seq_group_block_size - swap_out_block_num
    #             self._insert_seq_group_into_partial_swapped(
    #                 r_bs, seq_group_request_id, seq_group)
    #             # return seq_group as the swap out seq group
    #             swapped_out_seq_groups[seq_group] = swap_out_block_num
    #             return True, swapped_out_seq_groups
    #         else:
    #             total_swap_block = 0
    #             last_swap_block = 0
    #             selected_partial_swapped_sg = partial_swapped_sgs[:
    #                                                               selected_swapped_sg_index]
    #             for selected_seq_group in selected_partial_swapped_sg:
    #                 r_bs,  partial_swapped_sg = \
    #                     self._get_seq_group_from_partial_swapped(selected_seq_group)
    #                 last_swap_block = total_swap_block
    #                 total_swap_block += r_bs
    #                 if total_swap_block > seq_group_block_size:
    #                     block_unit = max(
    #                         ceil(partial_swapped_sg.total_token_block_size *
    #                              swap_out_rate), 1)
    #                     swap_out_block_size = ceil(
    #                         (seq_group_block_size - last_swap_block) /
    #                         block_unit) * block_unit
    #                     left_block_size = r_bs - swap_out_block_size
    #                     if left_block_size > 0:
    #                         self._insert_seq_group_into_partial_swapped(
    #                             left_block_size, selected_seq_group,
    #                             partial_swapped_sg)
    #                     if swap_out_block_size > 0:
    #                         swapped_out_seq_groups[
    #                             partial_swapped_sg] = swap_out_block_size
    #                 else:
    #                     swapped_out_seq_groups[partial_swapped_sg] = r_bs
    #             return False, swapped_out_seq_groups

    def _swap_out_partial(
            self, seq_group_request_id: str, seq_group: SequenceGroup,
            budget: SchedulingBudget,
            num_running_tokens: int) -> Tuple[bool, Dict[SequenceGroup, int]]:
        """Swap out a sequence group partially.

        Args:
            seq_group: The sequence group to swap out.
            budget: The scheduling budget.
            block_size: The size of the block to swap out.
        Returns:
            A tuple of (is_swap_out, 
                        swapped_out_seq_groups, 
                        swap_out_block_nums).
            is_swap_out: True if the sequence group is swapped out, 
                        False otherwise.
        """
        swapped_out_seq_groups: Dict[SequenceGroup, int] = {}
        seq_group_token_num = num_running_tokens
        if len(self.waiting) > 0:
            # swap out all blocks for the prefill request
            budget.subtract_num_batched_tokens(seq_group_request_id,
                                               seq_group_token_num)
            swapped_out_seq_groups[seq_group] = -1 # swap out the whole seq_group
            return True, swapped_out_seq_groups
        swap_out_rate = self.scheduler_config.swap_out_partial_rate
        # Swap out a partial block.
        seq_group_block_size = seq_group.total_token_block_size
        block_unit = max(ceil(seq_group_block_size * swap_out_rate), 1)
        if len(self.partial_swapped) == 0:
            # swap out part of the current seq_group for decode request
            swap_out_block_num = block_unit
            budget.subtract_num_batched_tokens(seq_group_request_id,
                                               seq_group_token_num)
            r_bs = seq_group_block_size - swap_out_block_num
            if r_bs > 0:
                self._insert_seq_group_into_partial_swapped(
                    r_bs, seq_group_request_id, seq_group)
            swapped_out_seq_groups[seq_group] = swap_out_block_num
            return True, swapped_out_seq_groups
        else:
            # swap out left part of the sequence group in the
            # partial_swapped queue prior to the current seq_group
            partial_swapped_values = self.partial_swapped_values
            partial_swapped_values.sort(key=lambda x: x[0])
            partial_swapped_bn, partial_swapped_sgs = map(
                list, zip(*partial_swapped_values))
            
            # potential bug
            selected_swapped_sg_index = self.min_numbers_sum_at_least(
                partial_swapped_bn, seq_group_block_size)
            if selected_swapped_sg_index == -1:
                # swap out part of the current seq_group for decode request due to the lack of free blocks from partial_swapped queue
                block_unit = max(int(seq_group_block_size * swap_out_rate), 1)
                swap_out_block_num = block_unit
                budget.subtract_num_batched_tokens(seq_group_request_id,
                                                   seq_group_token_num)
                r_bs = seq_group_block_size - swap_out_block_num
                self._insert_seq_group_into_partial_swapped(
                    r_bs, seq_group_request_id, seq_group)
                # return seq_group as the swap out seq group
                swapped_out_seq_groups[seq_group] = swap_out_block_num
                return True, swapped_out_seq_groups
            # potential bug
            
            else:
                total_swap_block = 0
                last_swap_block = 0
                selected_partial_swapped_sg = partial_swapped_sgs[:
                                                                  selected_swapped_sg_index]
                for selected_seq_group in selected_partial_swapped_sg:
                    r_bs,  partial_swapped_sg = \
                        self._get_seq_group_from_partial_swapped(selected_seq_group)
                    last_swap_block = total_swap_block
                    total_swap_block += r_bs
                    if total_swap_block > seq_group_block_size:
                        block_unit = max(
                            ceil(partial_swapped_sg.total_token_block_size *
                                 swap_out_rate), 1)
                        swap_out_block_size = ceil(
                            (seq_group_block_size - last_swap_block) /
                            block_unit) * block_unit
                        left_block_size = r_bs - swap_out_block_size
                        if left_block_size > 0:
                            self._insert_seq_group_into_partial_swapped(
                                left_block_size, selected_seq_group,
                                partial_swapped_sg)
                        if swap_out_block_size > 0:
                            swapped_out_seq_groups[
                                partial_swapped_sg] = swap_out_block_size
                    else:
                        swapped_out_seq_groups[partial_swapped_sg] = r_bs
                return False, swapped_out_seq_groups

    def _append_seq_group(self,
                          seq_group: SequenceGroup,
                          blocks_to_copy: List[Tuple[int, int]],
                          num_running_tokens: int,
                          prefill_seq_groups: List[ScheduledSequenceGroup],
                          decode_seq_groups: List[ScheduledSequenceGroup],
                          budget: SchedulingBudget,
                          curr_loras: Optional[Set[int]],
                          enable_chunking: bool = False) -> None:
        total_block_size = seq_group.total_token_block_size
        self._append_slots(seq_group, blocks_to_copy)
        is_prefill = seq_group.is_prefill()
        if is_prefill:
            prefill_seq_groups.append(
                ScheduledSequenceGroup(seq_group=seq_group,
                                       token_chunk_size=num_running_tokens))
        else:
            decode_seq_groups.append(
                ScheduledSequenceGroup(seq_group=seq_group,
                                       token_chunk_size=1))
        
        if self.seq_group_for_preempted==() or total_block_size > self.seq_group_for_preempted[1]:
            self.seq_group_for_preempted = (seq_group, total_block_size)
        
        budget.add_num_batched_tokens(seq_group.request_id, num_running_tokens)
        seq_group.reset_waiting_iter_nums()
        # seq_group.update_execution_iter_nums()
        # OPTIMIZATION:  Note that get_max_num_running_seqs is
        # expensive. For the default scheduling chase where
        # enable_chunking is False, num_seqs are updated before running
        # this method, so we don't have to update it again here.
        if enable_chunking:
            num_running_seqs = seq_group.get_max_num_running_seqs()
            budget.add_num_seqs(seq_group.request_id, num_running_seqs)
        if curr_loras is not None and seq_group.lora_int_id > 0:
            curr_loras.add(seq_group.lora_int_id)
    
    def _schedule_infer(
        self,
        running_queue: deque,
        budget: SchedulingBudget,
        curr_loras: Optional[Set[int]],
        policy: Policy,
        enable_chunking: bool = False,
    ) -> Tuple[deque, SchedulerRunningOutputs, int]:
        blocks_to_swap_out: List[Tuple[int, int]] = []
        blocks_to_copy: List[Tuple[int, int]] = []

        decode_seq_groups: List[ScheduledSequenceGroup] = []
        prefill_seq_groups: List[ScheduledSequenceGroup] = []
        recomputed_token_nums: int = 0
        preempted: Set[SequenceGroup] = set()
        swapped_out: Set[SequenceGroup] = set()
        
        if self.scheduler_config.policy == "tfittradeoff":
            running_queue = policy.sorted_by_priority(1, running_queue, -1)
        else:
            running_queue = policy.sort_by_priority(time.time(), running_queue)
            
        all_token_block_size = sum([part[0] for part in self.partial_swapped.values()])  # self.partial_swapped: Dict[str, Tuple[int, SequenceGroup]] = {}
        
        while running_queue:
            seq_group: SequenceGroup = running_queue[0]
            all_token_block_size += seq_group.total_token_block_size
            num_running_tokens = self._get_num_new_tokens(
                seq_group, SequenceStatus.RUNNING, enable_chunking, budget)
            
            if num_running_tokens == 0:
                break
            
            running_queue.popleft()
            seq_group_request_id = seq_group.request_id
            
            # while not self._can_allocate_seq(all_token_block_size):
            while not self._can_allocate_seq(all_token_block_size):
                if self.partial_swap_out_flag:
                    (seq_group_swapped_out_flag,
                    swapped_out_seq_groups) = self._swap_out_partial(
                        seq_group_request_id, seq_group, budget,
                        num_running_tokens)
                        
                    
                    swapped_out_seq_groups_items = swapped_out_seq_groups.items(
                    )
                    
                    for swap_out_seq_group, swap_out_block_nums in swapped_out_seq_groups_items:
                        if swap_out_block_nums == -1:
                            seq_group_status = SequenceStatus.SWAPPED
                        else:
                            seq_group_status = SequenceStatus.PARTIAL_SWAPPED
                            
                        preempted_mode = self._preempt(
                            swap_out_seq_group,
                            blocks_to_swap_out,
                            self.preemption_mode,
                            swap_out_block_nums=swap_out_block_nums,
                            seq_group_status=seq_group_status)
                        
                        if preempted_mode == PreemptionMode.RECOMPUTE:
                            preempted.add(swap_out_seq_group)
                        else:
                            swapped_out.add(swap_out_seq_group)
                        
                        if swap_out_block_nums == -1:
                            all_token_block_size -= swap_out_seq_group.total_token_block_size
                        else:
                            all_token_block_size -= swap_out_block_nums
                        
                        # if seq_group_status == SequenceStatus.SWAPPED:
                        #     all_token_block_size -= swap_out_seq_group.total_token_block_size
                        # else:
                        #     _total_token_block_size = sum([seq.logical_token_block_size for seq in swap_out_seq_group.get_seqs(status=SequenceStatus.PARTIAL_SWAPPED)])
                        #     # if seq_group.is_prefill():
                        #     #     _total_token_block_size += 1
                        #     # else:
                        #     #     _total_token_block_size += len(seq_group.get_seqs(status=SequenceStatus.PARTIAL_SWAPPED))
                        #     all_token_block_size -= _total_token_block_size
                        
                        if swap_out_block_nums == -1:
                            self.total_swap_out_blocks += swap_out_seq_group.total_token_block_size
                        else:
                            self.total_swap_out_blocks += swap_out_block_nums
                        
                        # self.total_swap_out_blocks += swap_out_block_nums
                        self.total_swap_out_seqs += 1
                        
                    if seq_group_swapped_out_flag:
                        print(f"Current seq_group {seq_group.get_seqs()} is swapped or partially swapped, break")
                        break
                        
                    # if not seq_group_swapped_out_flag:
                    #     self._append_seq_group(seq_group, blocks_to_copy,
                    #                         num_running_tokens,
                    #                         prefill_seq_groups,
                    #                         decode_seq_groups, budget,
                    #                         curr_loras, enable_chunking)
                    
                    # if not seq_group_swapped_out_flag:
                    #     print("Current SQ hasn't been swapped out, check GPU space again")
                    #     continue
                    #     # self._append_seq_group(seq_group, blocks_to_copy,
                    #     #                     num_running_tokens,
                    #     #                     prefill_seq_groups,
                    #     #                     decode_seq_groups, budget,
                    #     #                     curr_loras, enable_chunking)
                    # else:
                        
                    #     break
     
                else:
                    budget.subtract_num_batched_tokens(seq_group_request_id,
                                                    num_running_tokens)
                    swap_out_seq_group = seq_group
                    num_running_seqs = seq_group.get_max_num_running_seqs()
                    seq_group.update_waiting_iter_nums()
                    budget.subtract_num_seqs(seq_group_request_id,
                                            num_running_seqs)
                    preempted_mode = self._preempt(swap_out_seq_group,
                                                blocks_to_swap_out,
                                                self.preemption_mode)

                    if preempted_mode == PreemptionMode.RECOMPUTE:
                        preempted.add(swap_out_seq_group)
                    else:
                        swapped_out.add(swap_out_seq_group)
                        
                    all_token_block_size -= swap_out_seq_group.total_token_block_size
                    self.total_swap_out_blocks += swap_out_seq_group.total_token_block_size
                    self.total_swap_out_seqs += 1
                    break
                
            else:
                self._append_seq_group(seq_group, blocks_to_copy,
                                        num_running_tokens, prefill_seq_groups,
                                        decode_seq_groups, budget, curr_loras,
                                        enable_chunking)

        if len(swapped_out) > 0:
            total_swapped_out: Set[SequenceGroup] = set(self.swapped)
            swapped_out = swapped_out.difference(total_swapped_out)
        return running_queue, SchedulerRunningOutputs(
            decode_seq_groups=decode_seq_groups,
            prefill_seq_groups=prefill_seq_groups,
            preempted=list(preempted),
            swapped_out=list(swapped_out),
            blocks_to_swap_out=blocks_to_swap_out,
            blocks_to_copy=blocks_to_copy,
            num_lookahead_slots=self._get_num_lookahead_slots(
                is_prefill=False)), recomputed_token_nums

    # def _schedule_infer(
    #     self,
    #     running_queue: deque,
    #     budget: SchedulingBudget,
    #     curr_loras: Optional[Set[int]],
    #     policy: Policy,
    #     enable_chunking: bool = False,
    # ) -> Tuple[deque, SchedulerRunningOutputs, int]:
    #     blocks_to_swap_out: List[Tuple[int, int]] = []
    #     blocks_to_copy: List[Tuple[int, int]] = []

    #     decode_seq_groups: List[ScheduledSequenceGroup] = []
    #     prefill_seq_groups: List[ScheduledSequenceGroup] = []
    #     recomputed_token_nums: int = 0
    #     preempted: Set[SequenceGroup] = set()
    #     swapped_out: Set[SequenceGroup] = set()
        
    #     if self.scheduler_config.policy == "tfittradeoff":
    #         running_queue = policy.sorted_by_priority(1, running_queue, -1)
    #     else:
    #         running_queue = policy.sort_by_priority(time.time(), running_queue)
            
    #     all_token_block_size = sum([part[0] for part in self.partial_swapped.values()])  # self.partial_swapped: Dict[str, Tuple[int, SequenceGroup]] = {}
        
    #     while running_queue:
    #         seq_group: SequenceGroup = running_queue[0]
    #         all_token_block_size += seq_group.total_token_block_size
    #         print("About to schedule: ", seq_group.get_seqs())
    #         num_running_tokens = self._get_num_new_tokens(
    #             seq_group, SequenceStatus.RUNNING, enable_chunking, budget)
            
    #         if num_running_tokens == 0:
    #             break
            
    #         running_queue.popleft()
    #         seq_group_request_id = seq_group.request_id
            
    #         while not self._can_allocate_seq(all_token_block_size):
    #             # swap out the seq_group in the partial_swapped
    #             print("No gpu space")
    #             if self.partial_swapped:

    #                 victim_seq_group_request_id, (victim_token_block_size, victim_seq_group) = self.partial_swapped.popitem()
    #                 print("victim_seq_group: ", victim_seq_group.get_seqs())
                    
    #                 self._get_seq_group_from_partial_swapped()

    #                 swap_out_seq_group = victim_seq_group
    #                 # victim_seq_group.update_waiting_iter_nums()
                    
    #                 swap_out_block_nums = victim_token_block_size
    #                 seq_group_status = SequenceStatus.SWAPPED
    #                 preempted_mode = self._preempt(swap_out_seq_group,
    #                                             blocks_to_swap_out,
    #                                             self.preemption_mode,
    #                                             swap_out_block_nums,
    #                                             seq_group_status)
    #                 print("victim info after preempting:", swap_out_seq_group.get_seqs())
    #                 for seq in swap_out_seq_group.get_seqs(status=SequenceStatus.PARTIAL_SWAPPED):
    #                     seq.status = seq_group_status

    #                 if preempted_mode == PreemptionMode.RECOMPUTE:
    #                     preempted.add(swap_out_seq_group)
    #                 else:
    #                     swapped_out.add(swap_out_seq_group)
                        
    #                 all_token_block_size -= swap_out_seq_group.total_token_block_size
    #                 self.total_swap_out_blocks += swap_out_seq_group.total_token_block_size
    #                 self.total_swap_out_seqs += 1  
    #                 print("swap out the victim partial_swapped_seq_group successfully")
                    
    #             else:
    #                 # partial_swapped is empty, swap out the current seq_group 
    #                 if self.partial_swap_out_flag:
    #                     (seq_group_swapped_out_flag,
    #                     swapped_out_seq_groups) = self._swap_out_partial(
    #                         seq_group_request_id, seq_group, budget,
    #                         num_running_tokens)
    #                     swapped_out_seq_groups_items = swapped_out_seq_groups.items(
    #                     )
                        
                        
    #                     for swap_out_seq_group, swap_out_block_nums in swapped_out_seq_groups_items:
    #                         if swap_out_block_nums == -1:
    #                             seq_group_status = SequenceStatus.SWAPPED
    #                         else:
    #                             seq_group_status = SequenceStatus.PARTIAL_SWAPPED
                                
    #                         preempted_mode = self._preempt(
    #                             swap_out_seq_group,
    #                             blocks_to_swap_out,
    #                             self.preemption_mode,
    #                             swap_out_block_nums=swap_out_block_nums,
    #                             seq_group_status=seq_group_status)
                            
    #                         if preempted_mode == PreemptionMode.RECOMPUTE:
    #                             preempted.add(swap_out_seq_group)
    #                         else:
    #                             swapped_out.add(swap_out_seq_group)
                                
                                
    #                         if seq_group_status == SequenceStatus.SWAPPED:
    #                             all_token_block_size -= swap_out_seq_group.total_token_block_size
    #                         else:
    #                             _total_token_block_size = sum([seq.logical_token_block_size for seq in seq_group.get_seqs(status=SequenceStatus.PARTIAL_SWAPPED)])
    #                             if seq_group.is_prefill():
    #                                 _total_token_block_size + 1
    #                             else:
    #                                 _total_token_block_size + len(seq_group.get_seqs(status=SequenceStatus.PARTIAL_SWAPPED))
    #                             all_token_block_size -= _total_token_block_size
                                
    #                         self.total_swap_out_blocks += swap_out_seq_group.total_token_block_size
    #                         self.total_swap_out_seqs += 1
                        
    #                     if not seq_group_swapped_out_flag:
    #                         self._append_seq_group(seq_group, blocks_to_copy,
    #                                             num_running_tokens,
    #                                             prefill_seq_groups,
    #                                             decode_seq_groups, budget,
    #                                             curr_loras, enable_chunking)
    #                         break
                        
    #                     print("The result after dealing with the current seq_group due to no partial_swapped and no space on GPU:", seq_group.get_seqs())
    #                     break
                        
                                
    #                 else:
    #                     budget.subtract_num_batched_tokens(seq_group_request_id,
    #                                                     num_running_tokens)
    #                     swap_out_seq_group = seq_group
    #                     num_running_seqs = seq_group.get_max_num_running_seqs()
    #                     seq_group.update_waiting_iter_nums()
    #                     budget.subtract_num_seqs(seq_group_request_id,
    #                                             num_running_seqs)
    #                     preempted_mode = self._preempt(swap_out_seq_group,
    #                                                 blocks_to_swap_out,
    #                                                 self.preemption_mode)

    #                     if preempted_mode == PreemptionMode.RECOMPUTE:
    #                         preempted.add(swap_out_seq_group)
    #                     else:
    #                         swapped_out.add(swap_out_seq_group)
                            
    #                     all_token_block_size -= swap_out_seq_group.total_token_block_size
    #                     self.total_swap_out_blocks += swap_out_seq_group.total_token_block_size
    #                     self.total_swap_out_seqs += 1
    #                     break
    #         else:
    #             self._append_seq_group(seq_group, blocks_to_copy,
    #                                     num_running_tokens, prefill_seq_groups,
    #                                     decode_seq_groups, budget, curr_loras,
    #                                     enable_chunking)

    #     print("schedule_all_running done!")
    #     if len(swapped_out) > 0:
    #         total_swapped_out: Set[SequenceGroup] = set(self.swapped)
    #         swapped_out = swapped_out.difference(total_swapped_out)
    #     return running_queue, SchedulerRunningOutputs(
    #         decode_seq_groups=decode_seq_groups,
    #         prefill_seq_groups=prefill_seq_groups,
    #         preempted=list(preempted),
    #         swapped_out=list(swapped_out),
    #         blocks_to_swap_out=blocks_to_swap_out,
    #         blocks_to_copy=blocks_to_copy,
    #         num_lookahead_slots=self._get_num_lookahead_slots(
    #             is_prefill=False)), recomputed_token_nums

    def _schedule_infer_preemption(
        self,
        running_queue: Deque[SequenceGroup],
        swapped_queue: Deque[SequenceGroup],
        waiting_queue: Deque[SequenceGroup],
        budget: SchedulingBudget,
        policy: Policy,
        enable_chunking: bool = False,
    ) -> Tuple[deque, deque, deque, SchedulerRunningOutputs,
               SchedulerSwappedInOutputs, SchedulerPrefillOutputs, int]:
        """Schedule sequence groups that are in inference stage.

        It schedules waiting requests as long as it fits `budget` and
        curr_loras <= max_lora from the scheduling config. The input arguments
        `budget` and `curr_loras` are updated based on scheduled seq_groups.

        Args:
            running_queue: The queue that contains running requests.
                The given arguments are NOT in-place modified.
            swapped_queue: The queue that contains swapped out requests.
                The given arguments are NOT in-place modified.
            waiting_queue: The queue that contains waiting requests.
                The given arguments are NOT in-place modified.
            budget: The scheduling budget. The argument is in-place updated
                when any requests are scheduled.
            enable_chunking: If True, seq group can be chunked and only a
                chunked number of tokens are scheduled  if
                `budget.num_batched_tokens` has not enough capacity to schedule
                all tokens.
            policy: The scheduling policy.
        Returns:
            A tuple of (
            running_queue, swapped_queue, waiting_queue,
            SchedulerRunningOutputs, SchedulerSwappedInOutputs, SchedulerPrefillOutputs, recomputed_token_nums).
        
        """
        # create a copy of queues to avoid in-place modification
        total_queue = deque(running_queue + swapped_queue + waiting_queue)
        total_waiting_queue = deque(swapped_queue + waiting_queue)
        self.enable_chunking = enable_chunking

        decode_seq_groups_running: List[SequenceGroup] = []
        decode_seq_groups_swapped: List[SequenceGroup] = []
        prefill_seq_groups_running: List[SequenceGroup] = []
        prefill_seq_groups_swapped: List[SequenceGroup] = []
        preempted_running: List[SequenceGroup] = []
        swapped_out_running: List[SequenceGroup] = []
        blocks_to_swap_in: List[Tuple[int, int]] = []
        blocks_to_swap_out: List[Tuple[int, int]] = []
        blocks_to_copy_running: List[Tuple[int, int]] = []
        blocks_to_copy_swapped: List[Tuple[int, int]] = []
        infeasible_seq_groups: List[SequenceGroup] = []
        ignored_seq_groups: List[SequenceGroup] = []
        seq_groups_prefill: List[SequenceGroup] = []
        num_lookahead_slots_running: int = 0
        num_lookahead_slots_swapped: int = 0
        num_lookahead_slots_prefill: int = 0
        recomputed_token_nums: int = 0

        scheduler_preemtion = SchedulerPreemption(
            decode_seq_groups_running=decode_seq_groups_running,
            decode_seq_groups_swapped=decode_seq_groups_swapped,
            prefill_seq_groups_running=prefill_seq_groups_running,
            prefill_seq_groups_swapped=prefill_seq_groups_swapped,
            preempted_running=preempted_running,
            swapped_out_running=swapped_out_running,
            blocks_to_swap_in=blocks_to_swap_in,
            blocks_to_swap_out=blocks_to_swap_out,
            blocks_to_copy_running=blocks_to_copy_running,
            blocks_to_copy_swapped=blocks_to_copy_swapped,
            infeasible_seq_groups=infeasible_seq_groups,
            ignored_seq_groups=ignored_seq_groups,
            seq_groups_prefill=seq_groups_prefill,
            num_lookahead_slots_running=num_lookahead_slots_running,
            num_lookahead_slots_swapped=num_lookahead_slots_swapped,
            num_lookahead_slots_prefill=num_lookahead_slots_prefill,
        )
        total_seq_groups_list = total_queue
        gpu_block_capacity = self.block_manager.gpu_block_capacity
        tmp_total_block_size = 0
        tmp_total_running_block_size = 0
        selected_running_seq_groups: List[SequenceGroup] = []
        selected_swapped_seq_groups: List[SequenceGroup] = []
        running_seq_group_nums = 0
        priorities = [
            seq_group.priority_rate for seq_group in running_queue
            if seq_group.priority_rate > 0
        ]
        avg_priorities = np.median(priorities) if len(priorities) > 0 else 1 
        if self.total_running_block_size+len(running_queue) > gpu_block_capacity:
            # only sort when the running block size is larger than the gpu block capacity
            running_queue = policy.sorted_by_priority(avg_priorities, running_queue, -1)
        for sg in running_queue:
            block_size = sg.total_token_block_size
            tmp_total_block_size += block_size
            if tmp_total_block_size <= gpu_block_capacity:
                sg.reset_waiting_iter_nums()
                selected_running_seq_groups.append(sg)
                running_seq_group_nums += 1
                tmp_total_running_block_size += block_size
            else:
                if sg.is_prefill():
                    tmp_total_block_size -= block_size
                sg.update_waiting_iter_nums()
                selected_swapped_seq_groups.append(sg)
                # total_waiting_queue.append(sg)
        if len(swapped_queue)+len(selected_swapped_seq_groups)+len(waiting_queue) !=0:
            # pending_swapped_rate = max(math.log(1+len(swapped_queue)+len(selected_swapped_seq_groups)+len(waiting_queue)), 0)
            pending_swapped_rate = len(waiting_queue)/(len(swapped_queue)+len(selected_swapped_seq_groups)+len(waiting_queue))
        # if len(swapped_queue)+len(selected_swapped_seq_groups) !=0:
        #     # pending_swapped_rate = max(math.log(1+len(swapped_queue)+len(selected_swapped_seq_groups)+len(waiting_queue)), 0)
        #     pending_swapped_rate = len(waiting_queue)/(len(swapped_queue)+len(selected_swapped_seq_groups))
        else:
            pending_swapped_rate = 0.0
        if len(selected_swapped_seq_groups) > 0 or tmp_total_block_size < gpu_block_capacity:
            # only sort when there are available blocks to swap in
            total_waiting_queue = policy.sorted_by_priority(
                avg_priorities, total_waiting_queue, pending_swapped_rate)
        for sg in total_waiting_queue:
            block_size = sg.total_token_block_size
            tmp_total_block_size += block_size
            if tmp_total_block_size <= gpu_block_capacity:
                sg.reset_waiting_iter_nums()
                selected_running_seq_groups.append(sg)
                running_seq_group_nums += 1
                tmp_total_running_block_size += block_size
            else:
                if sg.is_prefill():
                    tmp_total_block_size -= block_size
                sg.update_waiting_iter_nums()
        self.total_running_block_size = tmp_total_running_block_size
        self.avg_block_size = tmp_total_block_size / max(
            len(total_seq_groups_list), 1)
        for seq_group in selected_swapped_seq_groups:
            self._preempt_seq(
                seq_group=seq_group,
                budget=budget,
                schedule_preemption=scheduler_preemtion,
                running_queue=running_queue,
            )
        for seq_group in selected_running_seq_groups:
            _, _, recomputed_token_nums = self._allocate_seq(
                seq_group=seq_group,
                budget=budget,
                schedule_preemption=scheduler_preemtion,
                running_queue=running_queue,
                waiting_queue=waiting_queue,
                swapped_queue=swapped_queue,
                recomputed_token_nums=recomputed_token_nums,
            )
        running_scheduler_output = SchedulerRunningOutputs(
            decode_seq_groups=scheduler_preemtion.decode_seq_groups_running,
            prefill_seq_groups=scheduler_preemtion.prefill_seq_groups_running,
            preempted=scheduler_preemtion.preempted_running,
            swapped_out=scheduler_preemtion.swapped_out_running,
            blocks_to_swap_out=scheduler_preemtion.blocks_to_swap_out,
            blocks_to_copy=scheduler_preemtion.blocks_to_copy_running,
            num_lookahead_slots=scheduler_preemtion.num_lookahead_slots_running
        )
        swapped_scheduler_output = SchedulerSwappedInOutputs(
            decode_seq_groups=scheduler_preemtion.decode_seq_groups_swapped,
            prefill_seq_groups=scheduler_preemtion.prefill_seq_groups_swapped,
            blocks_to_swap_in=scheduler_preemtion.blocks_to_swap_in,
            blocks_to_copy=scheduler_preemtion.blocks_to_copy_swapped,
            num_lookahead_slots=scheduler_preemtion.
            num_lookahead_slots_swapped,
            infeasible_seq_groups=scheduler_preemtion.infeasible_seq_groups)
        waiting_scheduler_output = SchedulerPrefillOutputs(
            seq_groups=scheduler_preemtion.seq_groups_prefill,
            num_lookahead_slots=scheduler_preemtion.
            num_lookahead_slots_prefill,
            ignored_seq_groups=scheduler_preemtion.ignored_seq_groups)

        return (running_queue, swapped_queue, waiting_queue,
                running_scheduler_output, swapped_scheduler_output,
                waiting_scheduler_output, recomputed_token_nums)

    def _allocate_seq(self, seq_group: SequenceGroup, budget: SchedulingBudget,
                      schedule_preemption: SchedulerPreemption,
                      running_queue: deque, waiting_queue: deque,
                      swapped_queue: deque, recomputed_token_nums: int):
        if seq_group in running_queue:
            num_running_tokens = self._get_num_new_tokens(
                seq_group, SequenceStatus.RUNNING, self.enable_chunking,
                budget)
            self._append_slots(seq_group,
                               schedule_preemption.blocks_to_copy_running)
            seq_group.reset_waiting_iter_nums()
            is_prefill = seq_group.is_prefill()
            if is_prefill:
                schedule_preemption.prefill_seq_groups_running.append(
                    ScheduledSequenceGroup(
                        seq_group=seq_group,
                        token_chunk_size=num_running_tokens))
                recomputed_token_nums += num_running_tokens
            else:
                schedule_preemption.decode_seq_groups_running.append(
                    ScheduledSequenceGroup(seq_group=seq_group,
                                           token_chunk_size=1))
            budget.add_num_batched_tokens(seq_group.request_id,
                                          num_running_tokens)
            num_running_seqs = seq_group.get_max_num_running_seqs()
            budget.add_num_seqs(seq_group.request_id, num_running_seqs)
            running_queue.remove(seq_group)
        elif seq_group in swapped_queue:
            is_prefill = seq_group.is_prefill()
            alloc_status = self.block_manager.can_swap_in(
                seq_group, self._get_num_lookahead_slots(is_prefill))
            if alloc_status == AllocStatus.NEVER:
                logger.warning(
                    "Failing the request %s because there's not enough kv "
                    "cache blocks to run the entire sequence.",
                    seq_group.request_id)
                for seq in seq_group.get_seqs():
                    seq.status = SequenceStatus.FINISHED_IGNORED
                schedule_preemption.infeasible_seq_groups.append(seq_group)
                swapped_queue.remove(seq_group)
            # The total number of sequences in the RUNNING state should not
            # exceed the maximum number of sequences.
            num_new_seqs = seq_group.get_max_num_running_seqs()
            num_new_tokens = self._get_num_new_tokens(seq_group,
                                                      SequenceStatus.SWAPPED,
                                                      self.enable_chunking,
                                                      budget)

            if (num_new_tokens == 0
                    or not budget.can_schedule(num_new_tokens=num_new_tokens,
                                               num_new_seqs=num_new_seqs)):
                return False, "No enough budgets to run new sequences.", recomputed_token_nums
            swapped_queue.remove(seq_group)
            self._swap_in(seq_group, schedule_preemption.blocks_to_swap_in)
            self.total_swap_in_blocks += seq_group.total_token_block_size
            self.total_swap_in_seqs += 1
            self._append_slots(seq_group,
                               schedule_preemption.blocks_to_copy_swapped)
            if is_prefill:
                schedule_preemption.prefill_seq_groups_swapped.append(
                    ScheduledSequenceGroup(seq_group,
                                           token_chunk_size=num_new_tokens))
            else:
                schedule_preemption.decode_seq_groups_swapped.append(
                    ScheduledSequenceGroup(seq_group, token_chunk_size=1))
            budget.add_num_batched_tokens(seq_group.request_id, num_new_tokens)
            budget.add_num_seqs(seq_group.request_id, num_new_seqs)

        elif seq_group in waiting_queue:
            waiting_seqs = seq_group.get_seqs(status=SequenceStatus.WAITING)
            assert len(waiting_seqs) == 1, (
                "Waiting sequence group should have only one prompt "
                "sequence.")
            num_new_tokens = self._get_num_new_tokens(seq_group,
                                                      SequenceStatus.WAITING,
                                                      self.enable_chunking,
                                                      budget)

            prompt_limit = self._get_prompt_limit(seq_group)
            if num_new_tokens > prompt_limit:
                logger.warning(
                    "Input prompt (%d tokens) is too long"
                    " and exceeds limit of %d", num_new_tokens, prompt_limit)
                for seq in waiting_seqs:
                    seq.status = SequenceStatus.FINISHED_IGNORED
                schedule_preemption.ignored_seq_groups.append(seq_group)
                waiting_queue.remove(seq_group)

            # If the sequence group cannot be allocated, stop.
            can_allocate = self.block_manager.can_allocate(seq_group)
            if can_allocate == AllocStatus.NEVER:
                logger.warning(
                    "Input prompt (%d tokens) is too long"
                    " and exceeds the capacity of block_manager",
                    num_new_tokens)
                for seq in waiting_seqs:
                    seq.status = SequenceStatus.FINISHED_IGNORED
                schedule_preemption.ignored_seq_groups.append(seq_group)
                waiting_queue.remove(seq_group)
            elif can_allocate == AllocStatus.LATER:
                return False, "Cannot allocate sequence group in the waiting queue.", recomputed_token_nums
            num_new_seqs = seq_group.get_max_num_running_seqs()
            if (num_new_tokens == 0
                    or not budget.can_schedule(num_new_tokens=num_new_tokens,
                                               num_new_seqs=num_new_seqs)):
                return False, "No enough budgets to run new sequences in the waiting queue.", recomputed_token_nums
            try:
                self._allocate_and_set_running(seq_group)
            except Exception:
                return False, "Failed to allocate sequence group.", recomputed_token_nums
            waiting_queue.remove(seq_group)
            schedule_preemption.seq_groups_prefill.append(
                ScheduledSequenceGroup(seq_group=seq_group,
                                       token_chunk_size=num_new_tokens))
            budget.add_num_batched_tokens(seq_group.request_id, num_new_tokens)
            budget.add_num_seqs(seq_group.request_id, num_new_seqs)
        return True, None, recomputed_token_nums

    def _preempt_seq(self, seq_group: SequenceGroup, budget: SchedulingBudget,
                     schedule_preemption: SchedulerPreemption,
                     running_queue: deque):
        preemption_mode = self.preemption_mode
        if seq_group in running_queue:
            if not self.block_manager.can_swap_out(seq_group):
                preemption_mode = PreemptionMode.RECOMPUTE
            num_running_tokens = self._get_num_new_tokens(
                seq_group, SequenceStatus.RUNNING, self.enable_chunking,
                budget)
            budget.subtract_num_batched_tokens(seq_group.request_id,
                                               num_running_tokens)
            num_running_seqs = seq_group.get_max_num_running_seqs()
            budget.subtract_num_seqs(seq_group.request_id, num_running_seqs)
            preempted_mode = self._preempt(
                seq_group, schedule_preemption.blocks_to_swap_out,
                preemption_mode)
            if preempted_mode == PreemptionMode.RECOMPUTE:
                schedule_preemption.preempted_running.append(seq_group)
            else:
                schedule_preemption.swapped_out_running.append(seq_group)
            running_queue.remove(seq_group)
            self.total_swap_out_seqs += 1 
            self.total_swap_out_blocks += seq_group.total_token_block_size
            # Queue requests that couldn't be scheduled.
        return True, None

    def _schedule_running_partial(
        self,
        running_queue: deque,
        budget: SchedulingBudget,
        curr_loras: Optional[Set[int]],
        policy: Policy,
        enable_chunking: bool = False,
        pending_swapped_rate: float = 0.0,
    ) -> Tuple[deque, SchedulerRunningOutputs, int]:
        '''
        Schedule sequence groups that are running and do not use LoRa.
        '''
        # Blocks that need to be swapped or copied before model execution.
        blocks_to_swap_out: List[Tuple[int, int]] = []
        blocks_to_copy: List[Tuple[int, int]] = []

        decode_seq_groups: List[ScheduledSequenceGroup] = []
        prefill_seq_groups: List[ScheduledSequenceGroup] = []
        recomputed_token_nums: int = 0
        preempted: Set[SequenceGroup] = set()
        swapped_out: Set[SequenceGroup] = set()
        # partial_swapped_flag = self.scheduler_config.swap_out_tokens_policy == "partial"
        partial_swapped_flag = self.partial_swap_out_flag
        partial_swapped_rate = self.scheduler_config.swap_out_partial_rate

        # NOTE(woosuk): Preemption happens only when there is no available slot
        # to keep all the sequence groups in the RUNNING state.
        # In this case, the policy is responsible for deciding which sequence
        # groups to preempt.
        now = time.time()
        if self.scheduler_config.policy == "tfittradeoff":
            running_queue = policy.sorted_by_priority(0, running_queue, pending_swapped_rate=pending_swapped_rate)
        else:
            running_queue = policy.sort_by_priority(now, running_queue)
        self.sort_time_iter += time.time() - now
        
        
        # if len(self.waiting)!=0:
        #     partial_swapped_flag = False
        # else:
        #     partial_swapped_flag = self.partial_swap_out_flag
        
        while running_queue:
            seq_group: SequenceGroup = running_queue[0]
            num_running_tokens = self._get_num_new_tokens(
                seq_group, SequenceStatus.RUNNING, enable_chunking, budget)

            if num_running_tokens == 0:
                break

            running_queue.popleft()
            required_block_size = seq_group.total_token_block_size

            while not self._can_append_slots(seq_group):
                swap_out_block_nums = -1
                budget.subtract_num_batched_tokens(seq_group.request_id,
                                                   num_running_tokens)
                num_running_seqs = seq_group.get_max_num_running_seqs()
                budget.subtract_num_seqs(seq_group.request_id,
                                         num_running_seqs)
                
                if running_queue:
                    # Preempt the lowest-priority sequence groups.
                    if not partial_swapped_flag:
                        victim_seq_group = running_queue.pop()

                        preempted_mode = self._preempt(victim_seq_group,
                                                       blocks_to_swap_out,
                                                       self.preemption_mode)
                        self.total_swap_out_blocks += victim_seq_group.total_token_block_size
                        self.total_swap_out_seqs += 1

                        if preempted_mode == PreemptionMode.RECOMPUTE:
                            preempted.add(victim_seq_group)
                        else:
                            swapped_out.add(victim_seq_group)
                    else:
                        # swap out part of the seq_group in the running_queue
                        # victim_seq_group = running_queue.pop() # Debug
                        # victim_seq_group_block_size = victim_seq_group.total_token_block_size
                        if len(self.partial_swapped) == 0:
                            victim_seq_group = running_queue.pop() # Debug
                            victim_seq_group_block_size = victim_seq_group.total_token_block_size
                            if victim_seq_group_block_size <= required_block_size:
                                swap_out_block_nums = -1
                                required_block_size -= victim_seq_group_block_size
                                seq_group_status = SequenceStatus.SWAPPED
                            else:
                                swap_out_block_unit = ceil(
                                    victim_seq_group_block_size *
                                    partial_swapped_rate)
                                swap_out_block_nums = max(
                                    ceil(required_block_size /
                                         swap_out_block_unit) *
                                    swap_out_block_unit, 1)
                                left_victim_block_size = victim_seq_group_block_size - swap_out_block_nums
                                
                                if left_victim_block_size > 0:
                                    victim_seq_group_request_id = victim_seq_group.request_id
                                    self.partial_swapped[
                                        victim_seq_group_request_id] = (
                                            left_victim_block_size,
                                            victim_seq_group)
                                    required_block_size = 0
                                    seq_group_status = SequenceStatus.PARTIAL_SWAPPED
                                else:
                                    swap_out_block_nums = -1
                                    required_block_size -= victim_seq_group_block_size
                                    seq_group_status = SequenceStatus.SWAPPED
                        else:
                            victim_seq_group_request_id = list(
                                self.partial_swapped.keys())[0]
                            left_victim_block_size, victim_seq_group = self.partial_swapped.pop(
                                victim_seq_group_request_id)
                            swap_out_block_unit = ceil(
                                victim_seq_group.total_token_block_size *
                                partial_swapped_rate)
                            if left_victim_block_size <= required_block_size:
                                swap_out_block_nums = left_victim_block_size
                                required_block_size -= left_victim_block_size
                                seq_group_status = SequenceStatus.SWAPPED
                            else:
                                swap_out_block_nums = max(
                                    ceil(required_block_size /
                                         swap_out_block_unit) *
                                    swap_out_block_unit, 1)
                                left_victim_block_size = left_victim_block_size - swap_out_block_nums
                                if left_victim_block_size > 0:
                                    self.partial_swapped[
                                        victim_seq_group_request_id] = (
                                            left_victim_block_size,
                                            victim_seq_group)
                                    required_block_size = 0
                                seq_group_status = SequenceStatus.PARTIAL_SWAPPED
                                
                        preempted_mode = self._preempt(
                            victim_seq_group,
                            blocks_to_swap_out,
                            self.preemption_mode,
                            swap_out_block_nums,
                            seq_group_status=seq_group_status)
                        
                        if preempted_mode == PreemptionMode.RECOMPUTE:
                            preempted.add(victim_seq_group)
                        else:
                            swapped_out.add(victim_seq_group)
                else:
                    # No other sequence groups can be preempted.
                    # Preempt the current sequence group.
                    if not partial_swapped_flag:
                        preempted_mode = self._preempt(seq_group,
                                                       blocks_to_swap_out,
                                                       self.preemption_mode)

                        if preempted_mode == PreemptionMode.RECOMPUTE:
                            preempted.add(seq_group)
                        else:
                            swapped_out.add(seq_group)
                    else:
                        victim_seq_group = seq_group
                        victim_seq_group_block_size = victim_seq_group.total_token_block_size
                        swap_out_block_unit = ceil(
                            victim_seq_group_block_size * partial_swapped_rate)
                        swap_out_block_nums = max(swap_out_block_unit, 1)
                        left_victim_block_size = victim_seq_group_block_size - swap_out_block_nums
                        victim_seq_group_request_id = victim_seq_group.request_id
                        self.partial_swapped[victim_seq_group_request_id] = (
                            left_victim_block_size, victim_seq_group)
                        seq_group_status = SequenceStatus.PARTIAL_SWAPPED
                        preempted_mode = self._preempt(
                            victim_seq_group,
                            blocks_to_swap_out,
                            self.preemption_mode,
                            swap_out_block_nums,
                            seq_group_status=seq_group_status)

                        if preempted_mode == PreemptionMode.RECOMPUTE:
                            preempted.add(victim_seq_group)
                        else:
                            swapped_out.add(victim_seq_group)
                    break
            else:
                self._append_seq_group(seq_group, blocks_to_copy,
                                       num_running_tokens, prefill_seq_groups,
                                       decode_seq_groups, budget, curr_loras,
                                       enable_chunking)

        if len(swapped_out) > 0:
            total_swapped_out = set(self.swapped)
            swapped_out = swapped_out.difference(total_swapped_out)
        
        self.schedule_running_time += time.time() - now
            
        return running_queue, SchedulerRunningOutputs(
            decode_seq_groups=decode_seq_groups,
            prefill_seq_groups=prefill_seq_groups,
            preempted=list(preempted),
            swapped_out=list(swapped_out),
            blocks_to_swap_out=blocks_to_swap_out,
            blocks_to_copy=blocks_to_copy,
            num_lookahead_slots=self._get_num_lookahead_slots(
                is_prefill=False)), recomputed_token_nums

    def _schedule_running(
        self,
        running_queue: deque,
        budget: SchedulingBudget,
        curr_loras: Optional[Set[int]],
        policy: Policy,
        # pending_swapped_rate: float,
        enable_chunking: bool = False,
    ) -> Tuple[deque, SchedulerRunningOutputs, int]:
        """Schedule sequence groups that are running.

        Running queue should include decode and chunked prefill requests.

        Args:
            running_queue: The queue that contains running requests (i.e.,
                decodes). The given arguments are NOT in-place modified.
            budget: The scheduling budget. The argument is in-place updated
                when any decodes are preempted.
            curr_loras: Currently batched lora request ids. The argument is
                in-place updated when any decodes are preempted.
            policy: The sorting policy to sort running_queue.
            enable_chunking: If True, seq group can be chunked and only a
                chunked number of tokens are scheduled  if
                `budget.num_batched_tokens` has not enough capacity to schedule
                all tokens.
    
        Returns:
            A tuple of remaining running queue (should be always 0) after
            scheduling and SchedulerRunningOutputs.
        """
        # Blocks that need to be swapped or copied before model execution.
        blocks_to_swap_out: List[Tuple[int, int]] = []
        blocks_to_copy: List[Tuple[int, int]] = []

        decode_seq_groups: List[ScheduledSequenceGroup] = []
        prefill_seq_groups: List[ScheduledSequenceGroup] = []
        recomputed_token_nums: int = 0
        preempted: List[SequenceGroup] = []
        swapped_out: List[SequenceGroup] = []

        # NOTE(woosuk): Preemption happens only when there is no available slot
        # to keep all the sequence groups in the RUNNING state.
        # In this case, the policy is responsible for deciding which sequence
        # groups to preempt.
        now = time.time()
        if self.scheduler_config.policy == "tfittradeoff":
            running_queue = policy.sorted_by_priority(1, running_queue, -1)
        else:
            running_queue = policy.sort_by_priority(now, running_queue)
            
        while running_queue:
            seq_group: SequenceGroup = running_queue[0]
            
            num_running_tokens = self._get_num_new_tokens(
                seq_group, SequenceStatus.RUNNING, enable_chunking, budget)

            if num_running_tokens == 0:
                break

            running_queue.popleft()

            while not self._can_append_slots(seq_group):
                budget.subtract_num_batched_tokens(seq_group.request_id,
                                                   num_running_tokens)
                num_running_seqs = seq_group.get_max_num_running_seqs()
                budget.subtract_num_seqs(seq_group.request_id,
                                         num_running_seqs)
                                         
                if curr_loras is not None and seq_group.lora_int_id > 0:
                    curr_loras.remove(seq_group.lora_int_id)

                if running_queue:
                    # Preempt the lowest-priority sequence groups.
                    victim_seq_group = running_queue.pop()

                    # logger.warning(
                    # "preemption mode: ", self.preemption_mode)
                    # self.total_swap_out_blocks += victim_seq_group.total_token_block_size
                    # self.total_swap_out_seqs += 1

                    if self.preemption_mode:
                        preempted_mode = self._preempt(victim_seq_group,
                                                       blocks_to_swap_out,
                                                       self.preemption_mode)
                    else:
                        preempted_mode = self._preempt(victim_seq_group,
                                                       blocks_to_swap_out,
                                                       None)

                    if preempted_mode == PreemptionMode.RECOMPUTE:
                        preempted.append(victim_seq_group)
                    else:
                        swapped_out.append(victim_seq_group)
                    victim_seq_group.swap_out_moment = time.time()
                    self.total_swap_out_blocks += victim_seq_group.total_token_block_size
                    self.total_swap_out_seqs += 1
                
                else:
                    # No other sequence groups can be preempted.
                    # Preempt the current sequence group.  

                    if self.preemption_mode:
                        preempted_mode = self._preempt(seq_group,
                                                       blocks_to_swap_out,
                                                       self.preemption_mode)
                    else:
                        preempted_mode = self._preempt(seq_group,
                                                       blocks_to_swap_out)

                    if preempted_mode == PreemptionMode.RECOMPUTE:
                        preempted.append(seq_group)
                    else:
                        swapped_out.append(seq_group)
                    self.total_swap_out_blocks += seq_group.total_token_block_size
                    self.total_swap_out_seqs += 1
                    seq_group.swap_out_moment = time.time()
                    break
            else:
                self._append_slots(seq_group, blocks_to_copy)
                seq_group.reset_waiting_iter_nums()
                is_prefill = seq_group.is_prefill()
                if is_prefill:
                    prefill_seq_groups.append(
                        ScheduledSequenceGroup(
                            seq_group=seq_group,
                            token_chunk_size=num_running_tokens))
                    recomputed_token_nums += num_running_tokens
                else:
                    decode_seq_groups.append(
                        ScheduledSequenceGroup(seq_group=seq_group,
                                               token_chunk_size=1))
                budget.add_num_batched_tokens(seq_group.request_id,
                                              num_running_tokens)
                # OPTIMIZATION:  Note that get_max_num_running_seqs is
                # expensive. For the default scheduling chase where
                # enable_chunking is False, num_seqs are updated before running
                # this method, so we don't have to update it again here.
                if enable_chunking:
                    num_running_seqs = seq_group.get_max_num_running_seqs()
                    budget.add_num_seqs(seq_group.request_id, num_running_seqs)
                if curr_loras is not None and seq_group.lora_int_id > 0:
                    curr_loras.add(seq_group.lora_int_id)
        
        

        return running_queue, SchedulerRunningOutputs(
            decode_seq_groups=decode_seq_groups,
            prefill_seq_groups=prefill_seq_groups,
            preempted=preempted,
            swapped_out=swapped_out,
            blocks_to_swap_out=blocks_to_swap_out,
            blocks_to_copy=blocks_to_copy,
            num_lookahead_slots=self._get_num_lookahead_slots(
                is_prefill=False)), recomputed_token_nums

    def _schedule_swapped(
        self,
        swapped_queue: deque,
        budget: SchedulingBudget,
        curr_loras: Optional[Set[int]],
        policy: Policy,
        pending_swapped_rate: float,
        enable_chunking: bool = False,
    ) -> Tuple[deque, SchedulerSwappedInOutputs]:
        """Schedule sequence groups that are swapped out.

        It schedules swapped requests as long as it fits `budget` and
        curr_loras <= max_lora from the scheduling config. The input arguments
        `budget` and `curr_loras` are updated based on scheduled seq_groups.

        Args:
            swapped_queue: The queue that contains swapped out requests.
                The given arguments are NOT in-place modified.
            budget: The scheduling budget. The argument is in-place updated
                when any requests are swapped in.
            curr_loras: Currently batched lora request ids. The argument is
                in-place updated when any requests are swapped in.
            policy: The sorting policy to sort swapped_queue.
            enable_chunking: If True, seq group can be chunked and only a
                chunked number of tokens are scheduled  if
                `budget.num_batched_tokens` has not enough capacity to schedule
                all tokens.

        Returns:
            A tuple of remaining swapped_queue after scheduling and
            SchedulerSwappedInOutputs.
        """
        # Blocks that need to be swapped or copied before model execution.
        blocks_to_swap_in: List[Tuple[int, int]] = []
        blocks_to_copy: List[Tuple[int, int]] = []
        decode_seq_groups: List[ScheduledSequenceGroup] = []
        prefill_seq_groups: List[ScheduledSequenceGroup] = []
        now = time.time()
        if self.scheduler_config.policy == "tfittradeoff":
            # priorities = [
            #     seq_group.priority for seq_group in swapped_queue
            #     if seq_group.priority > 0
            # ]
            priorities = [
                seq_group.priority_rate for seq_group in swapped_queue
                if seq_group.priority_rate > 0
            ]
            avg_priorities = np.mean(priorities) if len(priorities) > 0 else 1
            # swapped_queue = policy.sorted_by_priority(avg_priorities,
            #                                           swapped_queue, 1)
            swapped_queue = policy.sorted_by_priority(avg_priorities,
                                                      swapped_queue, pending_swapped_rate=pending_swapped_rate)
        else:
            swapped_queue = policy.sort_by_priority(now, swapped_queue)
        infeasible_seq_groups: List[SequenceGroup] = []
        leftover_swapped: Deque[SequenceGroup] = deque()
        st_while = time.time()
        while swapped_queue:
            seq_group: SequenceGroup = swapped_queue[0]
            # print("schedule current seq_group:", seq_group.get_seqs())
            # if self.scheduler_config.policy in ["infer", "tfittradeoff"] and seq_group.request_id == self.seq_group_for_preempted[
            #             0].request_id:
            if self.scheduler_config.policy in ["infer", "tfittradeoff"] and seq_group.request_id in self.partial_swapped:
                # seq_group.reset_execution_iter_nums()
                swapped_queue.popleft()
                leftover_swapped.appendleft(seq_group)
                continue
            # If the sequence group cannot be swapped in, stop.
            is_prefill = seq_group.is_prefill()
            alloc_status = self.block_manager.can_swap_in(
                seq_group, self._get_num_lookahead_slots(is_prefill))
            
            if alloc_status == AllocStatus.LATER:
                if self.scheduler_config.policy == "tfittradeoff": # Debug
                    seq_group.update_waiting_iter_nums()
                    swapped_queue.popleft()
                    leftover_swapped.appendleft(seq_group)
                    continue
                else:
                    for seq_group in swapped_queue:
                        seq_group.update_waiting_iter_nums()
                    break
                # swapped_queue.popleft()
                # leftover_swapped.appendleft(seq_group)
            elif alloc_status == AllocStatus.NEVER:
                logger.warning(
                    "Failing the request %s because there's not enough kv "
                    "cache blocks to run the entire sequence.",
                    seq_group.request_id)
                for seq in seq_group.get_seqs():
                    seq.status = SequenceStatus.FINISHED_IGNORED
                infeasible_seq_groups.append(seq_group)
                swapped_queue.popleft()
                continue

            lora_int_id = 0
            if self.lora_enabled:
                lora_int_id = seq_group.lora_int_id
                assert curr_loras is not None
                assert self.lora_config is not None
                if (lora_int_id > 0 and (lora_int_id not in curr_loras)
                        and len(curr_loras) >= self.lora_config.max_loras):
                    # We don't have a space for another LoRA, so
                    # we ignore this request for now.
                    leftover_swapped.appendleft(seq_group)
                    swapped_queue.popleft()
                    continue

            # The total number of sequences in the RUNNING state should not
            # exceed the maximum number of sequences.
            num_new_seqs = seq_group.get_max_num_running_seqs()
            num_new_tokens = self._get_num_new_tokens(seq_group,
                                                      SequenceStatus.SWAPPED,
                                                      enable_chunking, budget)
            if (num_new_tokens == 0
                    or not budget.can_schedule(num_new_tokens=num_new_tokens,
                                               num_new_seqs=num_new_seqs)):
                if self.scheduler_config.policy == "tfittradeoff": # Debug
                    seq_group.update_waiting_iter_nums()
                    swapped_queue.popleft()
                    leftover_swapped.appendleft(seq_group)
                    continue
                else:
                    for seq_group in swapped_queue:
                        seq_group.update_waiting_iter_nums()
                    break

            if lora_int_id > 0 and curr_loras is not None:
                curr_loras.add(lora_int_id)
            swapped_queue.popleft()
            
            if seq_group.metrics.waiting_iter_nums < seq_group.seq_len:
                self.total_low_eff_swap_out += 1
                self.total_low_eff_swap_out_diff += seq_group.seq_len - seq_group.metrics.waiting_iter_nums
                
            if seq_group.swap_out_moment != None:
                self.total_swap_out_waiting_time += time.time() - seq_group.swap_out_moment
                
            self._swap_in(seq_group, blocks_to_swap_in)
            
            self.total_swap_in_blocks += seq_group.total_token_block_size
            self.total_swap_in_seqs += 1
            
            seq_group_request_id = seq_group.request_id
            if seq_group_request_id in self.partial_swapped:
                _, _ = self.partial_swapped.pop(
                    seq_group_request_id)
                # if len(self.partial_swapped_values) > 0 and (
                #         left_block_size,
                #         seq_group_request_id) in self.partial_swapped_values:
                #     self.partial_swapped_values.remove(
                #         (left_block_size, seq_group_request_id))
            self._append_slots(seq_group, blocks_to_copy)
            is_prefill = seq_group.is_prefill()
            if is_prefill:
                prefill_seq_groups.append(
                    ScheduledSequenceGroup(seq_group,
                                           token_chunk_size=num_new_tokens))
            else:
                decode_seq_groups.append(
                    ScheduledSequenceGroup(seq_group, token_chunk_size=1))
            budget.add_num_batched_tokens(seq_group.request_id, num_new_tokens)
            budget.add_num_seqs(seq_group.request_id, num_new_seqs)
            seq_group.reset_waiting_iter_nums()
        self.swap_while += time.time() - st_while
        swapped_queue.extendleft(leftover_swapped)
        self.schedule_swapped_time += time.time() - now

        return swapped_queue, SchedulerSwappedInOutputs(
            decode_seq_groups=decode_seq_groups,
            prefill_seq_groups=prefill_seq_groups,
            blocks_to_swap_in=blocks_to_swap_in,
            blocks_to_copy=blocks_to_copy,
            num_lookahead_slots=self._get_num_lookahead_slots(
                is_prefill=False),
            infeasible_seq_groups=infeasible_seq_groups,
        )

    def _get_prompt_limit(self, seq_group: SequenceGroup) -> int:
        if self.scheduler_config.chunked_prefill_enabled:
            prompt_limit = self.scheduler_config.max_model_len
        else:
            prompt_limit = min(self.scheduler_config.max_model_len,
                               self.scheduler_config.max_num_batched_tokens)

        # Model is fine tuned with long context. Return the fine tuned max_len.
        if (seq_group.lora_request
                and seq_group.lora_request.long_lora_max_len):
            assert prompt_limit <= seq_group.lora_request.long_lora_max_len
            return seq_group.lora_request.long_lora_max_len
        else:
            return prompt_limit

    def _schedule_prefills(
        self,
        waiting_queue: deque,
        budget: SchedulingBudget,
        curr_loras: Optional[Set[int]],
        pending_swapped_rate: float,
        enable_chunking: bool = False,
        policy: Optional[Policy] = None,
    ) -> Tuple[deque, SchedulerPrefillOutputs]:
        """Schedule sequence groups that are in prefill stage.

        Note that the current scheduler treats PREEMPTED_FOR_RECOMPUTE
        as a new prefill (that starts from beginning -> most recently generated
        tokens).

        It schedules waiting requests as long as it fits `budget` and
        curr_loras <= max_lora from the scheduling config. The input arguments
        `budget` and `curr_loras` are updated based on scheduled seq_groups.

        Args:
            waiting_queue: The queue that contains prefill requests.
                The given arguments are NOT in-place modified.
            budget: The scheduling budget. The argument is in-place updated
                when any requests are scheduled.
            curr_loras: Currently batched lora request ids. The argument is
                in-place updated when any requests are scheduled.
            enable_chunking: If True, seq group can be chunked and only a
                chunked number of tokens are scheduled  if
                `budget.num_batched_tokens` has not enough capacity to schedule
                all tokens.

        Returns:
            A tuple of remaining waiting_queue after scheduling and
            SchedulerSwappedInOutputs.
        """
        now = time.time()
        ignored_seq_groups: List[SequenceGroup] = []
        seq_groups: List[SequenceGroup] = []
        # We don't sort waiting queue because we assume it is sorted.
        # Copy the queue so that the input queue is not modified.
        waiting_queue = deque([s for s in waiting_queue])
        if policy is not None:
            if self.scheduler_config.policy == "tfittradeoff":
                priorities = [
                    seq_group.priority for seq_group in waiting_queue
                    if seq_group.priority > 0
                ]
                avg_priorities = np.mean(
                    priorities) if len(priorities) > 0 else 1
                waiting_queue = policy.sorted_by_priority(
                    avg_priorities, waiting_queue, pending_swapped_rate)
            else:
                waiting_queue = policy.sort_by_priority(
                    time.time(), waiting_queue)
                
        leftover_waiting_sequences: Deque[SequenceGroup] = deque()
        
        st_while = time.time()
        while self._passed_delay(time.time()) and waiting_queue:
            seq_group = waiting_queue[0]
            waiting_seqs = seq_group.get_seqs(status=SequenceStatus.WAITING)
            assert len(waiting_seqs) == 1, (
                "Waiting sequence group should have only one prompt "
                "sequence.")
            num_new_tokens = self._get_num_new_tokens(seq_group,
                                                      SequenceStatus.WAITING,
                                                      enable_chunking, budget)
            if not enable_chunking:
                num_prompt_tokens = waiting_seqs[0].get_len()
                assert num_new_tokens == num_prompt_tokens

            prompt_limit = self._get_prompt_limit(seq_group)
            if num_new_tokens > prompt_limit:
                logger.warning(
                    "Input prompt (%d tokens) is too long"
                    " and exceeds limit of %d", num_new_tokens, prompt_limit)
                for seq in waiting_seqs:
                    seq.status = SequenceStatus.FINISHED_IGNORED
                ignored_seq_groups.append(seq_group)
                waiting_queue.popleft()
                continue

            # If the sequence group cannot be allocated, stop.
            can_allocate = self.block_manager.can_allocate(seq_group)
            if can_allocate == AllocStatus.LATER:
                # if self.scheduler_config.policy == "tfittradeoff": #  Debug
                #     leftover_waiting_sequences.appendleft(seq_group)
                #     waiting_queue.popleft()
                #     continue
                # else:
                    break
            elif can_allocate == AllocStatus.NEVER:
                logger.warning(
                    "Input prompt (%d tokens) is too long"
                    " and exceeds the capacity of block_manager",
                    num_new_tokens)
                for seq in waiting_seqs:
                    seq.status = SequenceStatus.FINISHED_IGNORED
                ignored_seq_groups.append(seq_group)
                waiting_queue.popleft()
                continue

            lora_int_id = 0
            if self.lora_enabled:
                lora_int_id = seq_group.lora_int_id
                assert curr_loras is not None
                assert self.lora_config is not None
                if (self.lora_enabled and lora_int_id > 0
                        and lora_int_id not in curr_loras
                        and len(curr_loras) >= self.lora_config.max_loras):
                    # We don't have a space for another LoRA, so
                    # we ignore this request for now.
                    leftover_waiting_sequences.appendleft(seq_group)
                    waiting_queue.popleft()
                    continue

            num_new_seqs = seq_group.get_max_num_running_seqs()
            if (num_new_tokens == 0
                    or not budget.can_schedule(num_new_tokens=num_new_tokens,
                                               num_new_seqs=num_new_seqs)):
                # if self.scheduler_config.policy == "tfittradeoff": # Debug
                #     leftover_waiting_sequences.appendleft(seq_group)
                #     waiting_queue.popleft()
                #     continue
                # else:
                    break

            # Can schedule this request.
            if curr_loras is not None and lora_int_id > 0:
                curr_loras.add(lora_int_id)
            waiting_queue.popleft()
            self._allocate_and_set_running(seq_group)
            
            if seq_group.swap_out_moment != None:
                self.total_swap_out_waiting_time += time.time() - seq_group.swap_out_moment
            
            seq_groups.append(
                ScheduledSequenceGroup(seq_group=seq_group,
                                       token_chunk_size=num_new_tokens))
            budget.add_num_batched_tokens(seq_group.request_id, num_new_tokens)
            budget.add_num_seqs(seq_group.request_id, num_new_seqs)
        self.prefill_while += time.time() - st_while
        # Queue requests that couldn't be scheduled.
        waiting_queue.extendleft(leftover_waiting_sequences)
        if len(seq_groups) > 0:
            self.prev_prompt = True
            
        self.schedule_waiting_time += time.time() - now

        return waiting_queue, SchedulerPrefillOutputs(
            seq_groups=seq_groups,
            ignored_seq_groups=ignored_seq_groups,
            num_lookahead_slots=self._get_num_lookahead_slots(is_prefill=True))

    def _schedule_default(self) -> SchedulerOutputs:
        """Schedule queued requests.
        
        The current policy is designed to optimize the throughput. First,
        it batches as many prefill requests as possible. And it schedules
        decodes. If there's a pressure on GPU memory, decode requests can
        be swapped or preempted.
        """
        # Include running requests to the budget.
        budget = SchedulingBudget(
            token_budget=self.scheduler_config.max_num_batched_tokens,
            max_num_seqs=self.scheduler_config.max_num_seqs,
        )
        # Make sure we include num running seqs before scheduling prefill,
        # so that we don't schedule beyond max_num_seqs for prefill.
        for seq_group in self.running:
            budget.add_num_seqs(seq_group.request_id,
                                seq_group.get_max_num_running_seqs())
        curr_loras = set(
            seq_group.lora_int_id for seq_group in self.running
            if seq_group.lora_int_id > 0) if self.lora_enabled else None

        remaining_waiting, prefills = (self.waiting,
                                       SchedulerPrefillOutputs.create_empty())
        remaining_running, running_scheduled = (
            self.running, SchedulerRunningOutputs.create_empty())
        remaining_swapped, swapped_in = (
            self.swapped, SchedulerSwappedInOutputs.create_empty())

        # If any requests are swapped, prioritized swapped requests.
        if not self.swapped:
            remaining_waiting, prefills = self._schedule_prefills(
                self.waiting, budget, curr_loras, enable_chunking=False)

        policy = PolicyFactory.get_policy(policy_name="fcfs")
        # Don't schedule decodes if prefills are scheduled.
        # NOTE: If `_schedule_prefills` doesn't enable chunking, self.running
        # only contains decode requests, not chunked prefills.
        if len(prefills.seq_groups) == 0:
            remaining_running, running_scheduled, _ = self._schedule_running(
                self.running,
                budget,
                curr_loras,
                policy,
                enable_chunking=False)

            # If any sequence group is preempted, do not swap in any sequence
            # group. because it means there's no slot for new running requests.
            if len(running_scheduled.preempted) + len(
                    running_scheduled.swapped_out) == 0:
                remaining_swapped, swapped_in = self._schedule_swapped(
                    self.swapped, budget, curr_loras, policy)

        assert (budget.num_batched_tokens <=
                self.scheduler_config.max_num_batched_tokens)
        assert budget.num_curr_seqs <= self.scheduler_config.max_num_seqs

        # Update waiting requests.
        self.waiting = remaining_waiting
        self.waiting.extendleft(running_scheduled.preempted)
        # Update new running requests.
        self.running = remaining_running
        self.running.extend([s.seq_group for s in prefills.seq_groups])
        self.running.extend(
            [s.seq_group for s in running_scheduled.decode_seq_groups])
        self.running.extend(
            [s.seq_group for s in swapped_in.decode_seq_groups])
        # Update swapped requests.
        self.swapped = remaining_swapped
        self.swapped.extend(running_scheduled.swapped_out)
        preempted = (len(running_scheduled.preempted) +
                     len(running_scheduled.swapped_out))

        # There should be no prefill from running queue because this policy
        # doesn't allow chunked prefills.
        assert len(running_scheduled.prefill_seq_groups) == 0
        assert len(swapped_in.prefill_seq_groups) == 0
        return SchedulerOutputs(
            scheduled_seq_groups=(prefills.seq_groups +
                                  running_scheduled.decode_seq_groups +
                                  swapped_in.decode_seq_groups),
            num_prefill_groups=len(prefills.seq_groups),
            num_batched_tokens=budget.num_batched_tokens,
            blocks_to_swap_in=swapped_in.blocks_to_swap_in,
            blocks_to_swap_out=running_scheduled.blocks_to_swap_out,
            blocks_to_copy=running_scheduled.blocks_to_copy +
            swapped_in.blocks_to_copy,
            ignored_seq_groups=prefills.ignored_seq_groups +
            swapped_in.infeasible_seq_groups,
            num_lookahead_slots=running_scheduled.num_lookahead_slots,
            running_queue_size=len(self.running),
            preempted=preempted,
            num_running_to_waiting=0,
            num_waiting_to_running=0,
            recomputed_token_nums=0,
        )

    def _schedule_chunked_prefill(self):
        """Schedule queued requests.
        
        Chunked prefill allows to chunk prefill requests, batch them together
        with decode requests. This policy 1. schedule as many decoding requests
        as possible. 2. schedule chunked prefill requests that are not
        finished. 3. schedule swapped request. 4. schedule new prefill
        requests.

        The policy can sustain the high GPU utilization because it can put
        prefill and decodes requests to the same batch, while it improves
        inter token latency because decodes requests don't need to blocked
        by prefill requests.
        """
        
        budget = SchedulingBudget(
            token_budget=self.scheduler_config.max_num_batched_tokens,
            max_num_seqs=self.scheduler_config.max_num_seqs,
        )
        curr_loras: Set[int] = set()
        if self.reach_ddl:
            scheduled_seq_groups: List[ScheduledSequenceGroup] = []
            for seq_group in (self.waiting + self.running + self.swapped):
                for seq in seq_group.get_seqs():
                    seq.status = SequenceStatus.FINISHED_STOPPED
                # num_running_tokens = self._get_num_new_tokens(
                #     seq_group, SequenceStatus.RUNNING, True, budget)
                scheduled_seq_groups.append(ScheduledSequenceGroup(seq_group=seq_group,
                                       token_chunk_size=None)) 
            self.waiting = deque()
            self.running = deque()
            self.swapped= deque()
            # self.partial_swapped = deque()
            return SchedulerOutputs(
                scheduled_seq_groups=[],
                num_prefill_groups=0,
                num_batched_tokens=budget.num_batched_tokens,
                blocks_to_swap_in=0,
                blocks_to_swap_out=0,
                blocks_to_copy=0,
                ignored_seq_groups=scheduled_seq_groups,
                num_lookahead_slots=0,
                running_queue_size=0,
                preempted=0,
                num_running_to_waiting=0,
                num_waiting_to_running=0,
                recomputed_token_nums=0,
            )
        remaining_waiting, prefills = (self.waiting,
                                    SchedulerPrefillOutputs.create_empty())
        remaining_running, running_scheduled = (
            self.running, SchedulerRunningOutputs.create_empty())
        remaining_swapped, swapped_in = (
            self.swapped, SchedulerSwappedInOutputs.create_empty())
        policy = PolicyFactory.get_policy(
            policy_name=self.scheduler_config.policy)
        
        
        if len(self.swapped)+len(self.waiting) !=0:
            pending_swapped_rate = len(self.waiting)/(len(self.swapped)+len(self.waiting))
        else:
            pending_swapped_rate = 0.0
        
        pending_swapped_rate = -1
        
        if self.scheduler_config.policy in ["infer"]:
            remaining_running, running_scheduled, recomputed_token_nums = \
                self._schedule_infer(self.running,
                                                budget,
                                                curr_loras,
                                                policy,
                                                enable_chunking=True)
            if len(running_scheduled.preempted) + len(
                    running_scheduled.swapped_out) == 0:
                remaining_swapped, swapped_in, = self._schedule_swapped(
                    self.swapped, budget, curr_loras, policy, pending_swapped_rate=pending_swapped_rate)
            
            remaining_waiting, prefills = self._schedule_prefills(
                self.waiting, budget, curr_loras, pending_swapped_rate=pending_swapped_rate, enable_chunking=True)
            
        
            # Schedule new prefills.
            
            # remaining_waiting, prefills = self._schedule_prefills(
            #     self.waiting, budget, curr_loras, enable_chunking=True)
            # # filling the budget with swapped out requests
            # remaining_swapped, swapped_in, = self._schedule_swapped(
            #     self.swapped, budget, curr_loras, policy)
        elif self.scheduler_config.policy in ["inferpreempt", "sjmlfq"]:
            (remaining_running, remaining_swapped, remaining_waiting,
            running_scheduled, swapped_in,
            prefills, recomputed_token_nums) = \
                self._schedule_infer_preemption(
                running_queue=self.running,
                swapped_queue=self.swapped,
                waiting_queue=self.waiting,
                budget=budget,
                policy=policy,
                enable_chunking=True,
            )
        elif self.scheduler_config.policy == "tfittradeoff":
            remaining_running, running_scheduled, recomputed_token_nums = \
                self._schedule_running_partial(self.running,
                                    budget,
                                    curr_loras,
                                    policy,
                                    # pending_swapped_rate=1,
                                    enable_chunking=True,
                                    pending_swapped_rate=-1)
            # Schedule swapped out requests.
            # If preemption happens, it means we don't have space for swap-in.
            if len(running_scheduled.preempted) + len(
                    running_scheduled.swapped_out) == 0:
                remaining_swapped, swapped_in, = self._schedule_swapped(
                    self.swapped, budget, curr_loras, policy, pending_swapped_rate=0.0)

            # Schedule new prefills.
            remaining_waiting, prefills = self._schedule_prefills(
                self.waiting, budget, curr_loras, pending_swapped_rate=0.0, enable_chunking=True)
        else:
            remaining_running, running_scheduled, recomputed_token_nums = \
                self._schedule_running_partial(self.running,
                                    budget,
                                    curr_loras,
                                    policy,
                                    # pending_swapped_rate=1,
                                    enable_chunking=True)
            # Schedule swapped out requests.
            # If preemption happens, it means we don't have space for swap-in.
            if len(running_scheduled.preempted) + len(
                    running_scheduled.swapped_out) == 0:
                remaining_swapped, swapped_in, = self._schedule_swapped(
                    self.swapped, budget, curr_loras, policy, pending_swapped_rate=pending_swapped_rate)

            # Schedule new prefills.
            remaining_waiting, prefills = self._schedule_prefills(
                self.waiting, budget, curr_loras, pending_swapped_rate=pending_swapped_rate, enable_chunking=True)
        # et = time.time()
        # print(f"schedule chunked prefill time: {et - st}")

        assert (budget.num_batched_tokens <=
                self.scheduler_config.max_num_batched_tokens)
        assert budget.num_curr_seqs <= self.scheduler_config.max_num_seqs

        # Update waiting requests.
        self.waiting = remaining_waiting
        self.waiting.extendleft(running_scheduled.preempted)
        # Update new running requests.
        self.running = remaining_running
        self.running.extend([s.seq_group for s in prefills.seq_groups])
        self.running.extend(
            [s.seq_group for s in running_scheduled.decode_seq_groups])
        self.running.extend(
            [s.seq_group for s in running_scheduled.prefill_seq_groups])
        self.running.extend(
            [s.seq_group for s in swapped_in.decode_seq_groups])
        self.running.extend(
            [s.seq_group for s in swapped_in.prefill_seq_groups])
        
        # Update swapped requests.
        self.swapped = remaining_swapped
        self.swapped.extend(running_scheduled.swapped_out)
        self.iter_nums += 1
        
        # Motivation:
        # 1) GPU Memory:
        memory_existed = 0
        memory_new_generated = 0
        
        scheduled_seq_groups = (prefills.seq_groups +
                                running_scheduled.prefill_seq_groups +
                                swapped_in.prefill_seq_groups +
                                running_scheduled.decode_seq_groups +
                                swapped_in.decode_seq_groups)

        for seq_group in [s.seq_group for s in scheduled_seq_groups]:
            memory_existed += sum([seq.get_len() for seq in seq_group.get_seqs() if not seq.is_finished()])
        
        for seq_group in [s.seq_group for s in (running_scheduled.decode_seq_groups +
                                swapped_in.decode_seq_groups)]:
            memory_new_generated += sum([1 for seq in seq_group.get_seqs() if not seq.is_finished()])
                
        memory_iter = memory_existed + memory_new_generated
        
        self.gpu_memory_iter = memory_iter
        # Normalization
        self.gpu_memory_iter /= (self.cache_config.block_size * self.cache_config.num_gpu_blocks)

        
        # 2) GPU computation:
        
        prefills_seq_groups = (prefills.seq_groups +
                                running_scheduled.prefill_seq_groups +
                                swapped_in.prefill_seq_groups)
        computation_iter = 0
        
        for seq_group in [s.seq_group for s in prefills_seq_groups]:
            computation_iter += sum([seq.get_len() for seq in seq_group.get_seqs() if not seq.is_finished()])
            
        computation_iter += memory_new_generated
        
        self.gpu_computation_iter = computation_iter
        
        return SchedulerOutputs(
            scheduled_seq_groups=scheduled_seq_groups,
            num_prefill_groups=(len(prefills.seq_groups) +
                                len(swapped_in.prefill_seq_groups) +
                                len(running_scheduled.prefill_seq_groups)),
            num_batched_tokens=budget.num_batched_tokens,
            blocks_to_swap_in=swapped_in.blocks_to_swap_in,
            blocks_to_swap_out=running_scheduled.blocks_to_swap_out,
            blocks_to_copy=running_scheduled.blocks_to_copy +
            swapped_in.blocks_to_copy,
            ignored_seq_groups=prefills.ignored_seq_groups,
            num_lookahead_slots=running_scheduled.num_lookahead_slots,
            running_queue_size=len(self.running),
            preempted=(len(running_scheduled.preempted) +
                    len(running_scheduled.swapped_out)),
            num_running_to_waiting=len(running_scheduled.preempted),
            num_waiting_to_running=len(running_scheduled.prefill_seq_groups),
            recomputed_token_nums=recomputed_token_nums,
        )
            
            

    def _schedule(self) -> SchedulerOutputs:
        """Schedule queued requests."""
        if self.scheduler_config.chunked_prefill_enabled:
            return self._schedule_chunked_prefill()
        else:
            return self._schedule_default()

    def _can_append_slots(self,
                          seq_group: SequenceGroup,
                          pre_allocated_slots_num: int = 0) -> bool:
        """Determine whether or not we have enough space in the KV cache to
        continue generation of the sequence group.
        """
        # It is True only for testing case to trigger artificial preemption.
        if (self.enable_artificial_preemption
                and random.uniform(0, 1) < ARTIFICIAL_PREEMPTION_PROB
                and self.artificial_preempt_cnt > 0):
            self.artificial_preempt_cnt -= 1
            return False

        # Appending slots only occurs in decoding.
        is_prefill = False

        return self.block_manager.can_append_slots(
            seq_group=seq_group,
            pre_allocated_slots_num=pre_allocated_slots_num,
            num_lookahead_slots=self._get_num_lookahead_slots(is_prefill),
        )

    def _can_append_slots_prefill(self, seq_group: SequenceGroup) -> bool:
        return self.block_manager.can_append_slots(
            seq_group=seq_group,
            num_lookahead_slots=self._get_num_lookahead_slots(is_prefill=True),
        )

    def _can_allocate_seq(self, block_size: int) -> bool:
        return self.block_manager.can_allocate_infer(block_size)

    def update_iter_time(self, iter_time: float) -> None:
        self.avg_iter_time = iter_time

    def schedule(self) -> Tuple[List[SequenceGroupMetadata], SchedulerOutputs]:
        # Schedule sequence groups.
        # This function call changes the internal states of the scheduler
        # such as self.running, self.swapped, and self.waiting.
        scheduler_outputs = self._schedule()
        now = time.time()
        # if not self.reach_ddl:
            # Create input data structures.
        seq_group_metadata_list: List[SequenceGroupMetadata] = []

        for i, scheduled_seq_group in enumerate(scheduler_outputs.scheduled_seq_groups):
            seq_group = scheduled_seq_group.seq_group
            token_chunk_size = scheduled_seq_group.token_chunk_size
            seq_group.maybe_set_first_scheduled_time(now)

            # seq_id -> SequenceData
            seq_data: Dict[int, SequenceData] = {}
            # seq_id -> physical block numbers
            block_tables: Dict[int, List[int]] = {}
            for seq in seq_group.get_seqs(status=SequenceStatus.RUNNING):
                seq_id = seq.seq_id
                seq_data[seq_id] = seq.data
                block_tables[seq_id] = self.block_manager.get_block_table(seq)
                self.block_manager.access_all_blocks_in_seq(seq, now)

            common_computed_block_nums = (
                self.block_manager.get_common_computed_block_ids(
                    seq_group.get_seqs(status=SequenceStatus.RUNNING)))

            do_sample = True
            if seq_group.is_prefill():
                seqs = seq_group.get_seqs()
                # Prefill has only 1 sequence.
                assert len(seqs) == 1
                # In the next iteration, all prompt tokens are not computed.
                # It means the prefill is chunked, and we don't need sampling.
                # NOTE: We use get_len instead of get_prompt_len because when
                # a sequence is preempted, prefill includes previous generated
                # output tokens.
                if (token_chunk_size + seqs[0].data.get_num_computed_tokens() <
                        seqs[0].data.get_len()):
                    do_sample = False

            # It assumes the scheduled_seq_groups is ordered by
            # prefill < decoding.
            is_prompt = seq_group.is_prefill()
            seq_group_metadata = SequenceGroupMetadata(
                request_id=seq_group.request_id,
                is_prompt=is_prompt,
                seq_data=seq_data,
                sampling_params=seq_group.sampling_params,
                block_tables=block_tables,
                do_sample=do_sample,
                pooling_params=seq_group.pooling_params,
                token_chunk_size=token_chunk_size,
                lora_request=seq_group.lora_request,
                computed_block_nums=common_computed_block_nums,
                state=seq_group.state,
                # `multi_modal_data` will only be present for the 1st comm
                # between engine and worker.
                # the subsequent comms can still use delta, but
                # `multi_modal_data` will be None.
                multi_modal_data=seq_group.multi_modal_data
                if scheduler_outputs.num_prefill_groups > 0 else None,
                eos_token_id=seq_group.eos_token_id)
            seq_group_metadata_list.append(seq_group_metadata)

        # Now that the batch has been created, we can assume all blocks in the
        # batch will have been computed before the next scheduling invocation.
        # This is because the engine assumes that a failure in model execution
        # will crash the vLLM instance / will not retry.
        for scheduled_seq_group in scheduler_outputs.scheduled_seq_groups:
            self.block_manager.mark_blocks_as_computed(
                scheduled_seq_group.seq_group)
        return seq_group_metadata_list, scheduler_outputs
        # else:
        #     seq_group_metadata_list: List[SequenceGroupMetadata] = []
        #     for i, scheduled_seq_group in enumerate(scheduler_outputs.scheduled_seq_groups):
        #         seq_group = scheduled_seq_group.seq_group
        #         token_chunk_size = scheduled_seq_group.token_chunk_size
        #         seq_group.maybe_set_first_scheduled_time(now)

        #         # seq_id -> SequenceData
        #         seq_data: Dict[int, SequenceData] = {}
        #         # seq_id -> physical block numbers
        #         block_tables: Dict[int, List[int]] = {}
        #         for seq in seq_group.get_seqs():
        #             seq_id = seq.seq_id
        #             seq_data[seq_id] = seq.data
        #             block_tables[seq_id] = self.block_manager.get_block_table(seq)
        #             self.block_manager.access_all_blocks_in_seq(seq, now)
        #         do_sample = True

        #         # It assumes the scheduled_seq_groups is ordered by
        #         # prefill < decoding.
        #         is_prompt = seq_group.is_prefill()
        #         seq_group_metadata = SequenceGroupMetadata(
        #             request_id=seq_group.request_id,
        #             is_prompt=is_prompt,
        #             seq_data=seq_data,
        #             sampling_params=seq_group.sampling_params,
        #             block_tables=block_tables,
        #             do_sample=do_sample,
        #             pooling_params=seq_group.pooling_params,
        #             token_chunk_size=1,
        #             lora_request=seq_group.lora_request,
        #             computed_block_nums=0,
        #             state=seq_group.state,
        #             # `multi_modal_data` will only be present for the 1st comm
        #             # between engine and worker.
        #             # the subsequent comms can still use delta, but
        #             # `multi_modal_data` will be None.
        #             multi_modal_data=seq_group.multi_modal_data
        #             if scheduler_outputs.num_prefill_groups > 0 else None,
        #             eos_token_id=seq_group.eos_token_id)
        #         seq_group_metadata_list.append(seq_group_metadata)

        #     # Now that the batch has been created, we can assume all blocks in the
        #     # batch will have been computed before the next scheduling invocation.
        #     # This is because the engine assumes that a failure in model execution
        #     # will crash the vLLM instance / will not retry.
        #     for scheduled_seq_group in scheduler_outputs.scheduled_seq_groups:
        #         self.block_manager.mark_blocks_as_computed(
        #             scheduled_seq_group.seq_group)
        #     return seq_group_metadata_list, scheduler_outputs
            # return None, scheduler_outputs
    def fork_seq(self, parent_seq: Sequence, child_seq: Sequence) -> None:
        self.block_manager.fork(parent_seq, child_seq)

    def free_seq(self, seq: Sequence) -> None:
        """Free a sequence from a block table."""
        self.block_manager.free(seq)

    def free_finished_seq_groups(self) -> None:
        running_queue_length = len(self.running)
        self.running = deque(seq_group for seq_group in self.running
                             if not seq_group.is_finished())
        if len(self.running) < running_queue_length:
            self.has_finished_seqs = True

    def _allocate_and_set_running(self, seq_group: SequenceGroup) -> None:
        self.block_manager.allocate(seq_group)
        for seq in seq_group.get_seqs(status=SequenceStatus.WAITING):
            seq.status = SequenceStatus.RUNNING
            seq.status_transmit = SequenceStatus.WAITING_TO_RUNNING

    def _append_slots(
        self,
        seq_group: SequenceGroup,
        blocks_to_copy: List[Tuple[int, int]],
    ) -> None:
        """Appends new slots to the sequences in the given sequence group.

        Args:
            seq_group (SequenceGroup): The sequence group containing the
                sequences to append slots to.
            blocks_to_copy (List[Tuple[int, int]]): A list of tuple of two
                ints, the first int is the source block index, and the second
                int is the destination block index. This list is updated with
                the new source and destination block indices for the appended
                slots.
        """
        num_lookahead_slots = self._get_num_lookahead_slots(is_prefill=False)

        for seq in seq_group.get_seqs(status=SequenceStatus.RUNNING):
            cows = self.block_manager.append_slots(seq, num_lookahead_slots)
            blocks_to_copy.extend(cows)

    def _preempt(
        self,
        seq_group: SequenceGroup,
        blocks_to_swap_out: List[Tuple[int, int]],
        preemption_mode: Optional[PreemptionMode] = None,
        swap_out_block_nums: int = -1,
        seq_group_status: SequenceStatus = SequenceStatus.SWAPPED
    ) -> PreemptionMode:
        # If preemption mode is not specified, we determine the mode as follows:
        # We use recomputation by default since it incurs lower overhead than
        # swapping. However, when the sequence group has multiple sequences
        # (e.g., beam search), recomputation is not currently supported. In
        # such a case, we use swapping instead.
        # FIXME(woosuk): This makes our scheduling policy a bit bizarre.
        # As swapped sequences are prioritized over waiting sequences,
        # sequence groups with multiple sequences are implicitly prioritized
        # over sequence groups with a single sequence.
        # TODO(woosuk): Support recomputation for sequence groups with multiple
        # sequences. This may require a more sophisticated CUDA kernel.
        if preemption_mode is None:
            if self.user_specified_preemption_mode is None:
                if seq_group.get_max_num_running_seqs() == 1 or not self.block_manager.can_swap_out(seq_group):
                    preemption_mode = PreemptionMode.RECOMPUTE
                else:
                    preemption_mode = PreemptionMode.SWAP
                    
            elif self.user_specified_preemption_mode == "swap" and self.block_manager.can_swap_out(seq_group):
                preemption_mode = PreemptionMode.SWAP
            else:
                preemption_mode = PreemptionMode.RECOMPUTE
        
        # blocks = self.block_manager._get_physical_blocks(seq_group)
        # # print(self.block_manager.can_swap_out(seq_group))
        # if len(blocks) > self.block_manager.cpu_allocator.get_num_free_blocks() - self.block_manager.num_total_cpu_blocks * 0.3:
        #     preemption_mode = PreemptionMode.RECOMPUTE

        if (self.num_cumulative_preemption % 50 == 0
                and self.num_cumulative_preemption > 0):
            logger.debug(
                "Sequence group %s is preempted by %s mode because there is "
                "not enough KV cache space. This can affect the end-to-end "
                "performance. Increase gpu_memory_utilization or "
                "tensor_parallel_size to provide more KV cache memory. "
                "total_num_cumulative_preemption=%d", seq_group.request_id,
                preemption_mode, self.num_cumulative_preemption + 1)
        self.num_cumulative_preemption += 1

        if preemption_mode == PreemptionMode.RECOMPUTE or not self.block_manager.can_swap_out(seq_group):
            self._preempt_by_recompute(seq_group)
        elif preemption_mode == PreemptionMode.SWAP:
            self._preempt_by_swap(seq_group,
                                  blocks_to_swap_out,
                                  swap_out_block_nums,
                                  seq_group_status=seq_group_status)
        return preemption_mode

    def _preempt_by_recompute(
        self,
        seq_group: SequenceGroup,
    ) -> None:
        seqs = seq_group.get_seqs(status=SequenceStatus.RUNNING)
        # assert len(seqs) == 1
        for seq in seqs:
            seq.status = SequenceStatus.WAITING
            seq.status_transmit = SequenceStatus.RUNNING_TO_WAITING
            self.free_seq(seq)
            seq.reset_state_for_recompute()

    def _preempt_by_swap(
            self,
            seq_group: SequenceGroup,
            blocks_to_swap_out: List[Tuple[int, int]],
            swap_out_block_nums: int = -1,
            seq_group_status: SequenceStatus = SequenceStatus.SWAPPED) -> None:
        self._swap_out(seq_group,
                       blocks_to_swap_out,
                       swap_out_block_nums,
                       seq_group_status=seq_group_status)

    def _swap_in(
        self,
        seq_group: SequenceGroup,
        blocks_to_swap_in: List[Tuple[int, int]],
    ) -> None:
        mapping = self.block_manager.swap_in(seq_group)
        blocks_to_swap_in.extend(mapping)
        seqs = seq_group.get_seqs(
            status=SequenceStatus.SWAPPED) + seq_group.get_seqs(
                status=SequenceStatus.PARTIAL_SWAPPED)
        
        # asyncio.run(self._async_swap(blocks_to_swap_in,[],[]))

        for seq in seqs:
            seq.status = SequenceStatus.RUNNING

    def _swap_out(
            self,
            seq_group: SequenceGroup,
            blocks_to_swap_out: List[Tuple[int, int]],
            swap_out_block_nums: int = -1,
            seq_group_status: SequenceStatus = SequenceStatus.SWAPPED) -> None:
        if not self.block_manager.can_swap_out(seq_group):
            # FIXME(woosuk): Abort the sequence group instead of aborting the
            # entire engine.
            # self._preempt_by_recompute(seq_group)
            raise RuntimeError(
                "Aborted due to the lack of CPU swap space. Please increase "
                "the swap space to avoid this error.")
            # return
        mapping = self.block_manager.swap_out(
            seq_group, swap_out_block_nums=swap_out_block_nums)
        blocks_to_swap_out.extend(mapping)

        # asyncio.run(self._async_swap([],blocks_to_swap_out,[]))
        for seq in seq_group.get_seqs(status=SequenceStatus.RUNNING):
            seq.status = seq_group_status

    def _passed_delay(self, now: float) -> bool:
        if self.prev_prompt:
            self.last_prompt_latency = now - self.prev_time
        self.prev_time, self.prev_prompt = now, False
        # Delay scheduling prompts to let waiting queue fill up
        if self.scheduler_config.delay_factor > 0 and self.waiting:
            earliest_arrival_time = min(
                [e.metrics.arrival_time for e in self.waiting])
            passed_delay = (
                (now - earliest_arrival_time) >
                (self.scheduler_config.delay_factor * self.last_prompt_latency)
                or not self.running)
        else:
            passed_delay = True
        return passed_delay

    def _get_num_lookahead_slots(self, is_prefill: bool) -> int:
        """The number of slots to allocate per sequence per step, beyond known
        token ids. Speculative decoding uses these slots to store KV activations
        of tokens which may or may not be accepted.

        Speculative decoding does not yet support prefill, so we do not perform
        lookahead allocation for prefill.
        """
        if is_prefill:
            return 0

        return self.scheduler_config.num_lookahead_slots

    def _get_num_new_tokens(self, seq_group: SequenceGroup,
                            status: SequenceStatus, enable_chunking: bool,
                            budget: SchedulingBudget) -> int:
        """Get the next new tokens to compute for a given sequence group
            that's in a given `status`.

        The API could chunk the number of tokens to compute based on `budget`
        if `enable_chunking` is True. If a sequence group has multiple
        sequences (e.g., running beam search), it means it is in decoding
        phase, so chunking doesn't happen.

        Returns 0 if the new token cannot be computed due to token budget.
        """
        num_new_tokens = 0
        if status == SequenceStatus.SWAPPED:
            seqs = seq_group.get_seqs(status=status) + seq_group.get_seqs(
                status=SequenceStatus.PARTIAL_SWAPPED)
        else:
            seqs = seq_group.get_seqs(status=status)
        for seq in seqs:
            num_new_tokens += seq.get_num_new_tokens()

        assert num_new_tokens > 0, f"{seq_group.get_seqs()}, \
                                    {seq_group.request_id}"  

        # Chunk if a running request cannot fit in.
        # If number of seq > 1, it means it is doing beam search in a
        # decode phase. Do not chunk in that case.
        if enable_chunking and len(seqs) == 1:
            num_new_tokens = min(num_new_tokens,
                                 budget.remaining_token_budget())
        return num_new_tokens

    def max_numbers_sum_at_most(self, numbers: List[int], target: int) -> int:
        prefix_sum = list(accumulate(numbers))

        # Use bisect_right for binary search
        index = bisect.bisect_right(prefix_sum, target)

        if index >= len(prefix_sum):
            return -1
        return index

    def min_numbers_sum_at_least(self, numbers: List[int], target: int) -> int:
        """
        Find the minimum sum of numbers that is at least `target`.
        """
        # Calculate prefix sums using accumulate
        prefix_sum = list(accumulate(numbers))

        # Use bisect_left for binary search
        index = bisect.bisect_left(prefix_sum, target)

        if index >= len(prefix_sum):
            return -1

        return index + 1  # return the number of elements needed
