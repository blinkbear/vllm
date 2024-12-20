from collections import deque
import numpy as np
import math
from typing import Deque

from vllm.sequence import SequenceGroup
import random


class Policy:

    def get_priority(
        self,
        now: float,
        seq_group: SequenceGroup,
    ) -> float:
        raise NotImplementedError

    def got_priority(
        self,
        avg_priorities: float,
        seq_group: SequenceGroup,
        pending_swapped_rate: float,
    ):
        pass

    def sort_by_priority(
        self,
        now: float,
        seq_groups: Deque[SequenceGroup],
    ) -> Deque[SequenceGroup]:
        return deque(
            sorted(
                seq_groups,
                key=lambda seq_group: self.get_priority(now, seq_group),
                reverse=True,
            ))

    def sorted_by_priority(
        self,
        avg_priorities: float,
        seq_groups: Deque[SequenceGroup],
        pending_swapped_rate: float,
    ) -> Deque[SequenceGroup]:
        return deque(
            sorted(
                seq_groups,
                key=lambda seq_group: self.got_priority(
                    avg_priorities, seq_group, pending_swapped_rate),
                reverse=True,
            ))


class FCFS(Policy):

    def get_priority(
        self,
        now: float,
        seq_group: SequenceGroup,
    ) -> float:
        return now - seq_group.metrics.arrival_time


class MLFQ(Policy):

    def get_priority(
        self,
        now: float,
        seq_group: SequenceGroup,
    ) -> float:
        raise NotImplementedError


class SkipJoinMLFQ(Policy):

    def __init__(self, quantum_ratio=2, starve_limit=2):
        self.quantum_ratio = quantum_ratio  # Q_i/Q_{i-1}
        self.starve_limit = 5  # change to iter num
        self.min_quantum = 2  # quantum of Q_1

    def get_highest_priority(self, first_iteration_time):
        priority_level = 1  # the highest priority
        quantum = self.min_quantum  # the minimum quantum

        while quantum <= first_iteration_time:
            priority_level += 1
            quantum *= self.quantum_ratio

        return priority_level

    def get_priority(self, now: float, seq_group: SequenceGroup) -> float:
        input_length = len(seq_group.prompt_token_ids)

        if not seq_group.current_priority:  # Have been assigned with a priority?
            seq_group.current_priority = self.get_highest_priority(
                input_length)
        else:
            if (now - seq_group.metrics.first_scheduled_time >
                (2**(seq_group.current_priority - 1)) * self.min_quantum
                    and not seq_group.promoted):
                seq_group.current_priority += 1
            elif seq_group.metrics.waiting_iter_nums >= self.starve_limit:
                seq_group.current_priority = 1  # Promote to highest priority (Q1)
                seq_group.promoted = 1  # has been promoted to the Q1

        return -seq_group.current_priority  # higher value means higher priority


class TFTLatencyTrade(Policy):

    def get_gittins_index(self, seq_group: SequenceGroup, eos_probs: float,
                          decoding_length: int):
        # gittins index is the probability of the job ending in the next interval
        # divided by the expected remaining length of the job.
        # Optimization for the request-level latency and ttft

        n = 15
        value = 1 - eos_probs
        eos_probs_in_next_interval = 1 - value**15
        expect_remaining_length = value * ((1 + n * value**(n + 1) - (n + 1) *
                                            (value**n)) / (((1 - value)**2)))
        gittins_index = eos_probs_in_next_interval / expect_remaining_length
        waiting_percent = seq_group.metrics.waiting_iter_nums**2 * math.sqrt(
            decoding_length)
        priority = gittins_index * (1 + waiting_percent)
        return priority

    def get_priority(
        self,
        now: float,
        seq_group: SequenceGroup,
    ) -> float:
        eos_token_probs = []
        decoding_length = 0
        # token_blocks = seq_group.total_token_block_size
        for _, seq in seq_group.seqs_dict.items():
            eos_token_probs.extend(seq.get_eos_token_prob())
            decoding_length += seq.get_output_len()
        max_eos_token_prob = max(eos_token_probs)
        if max_eos_token_prob == -1000.0:
            priority = 2000 - seq_group.seq_len
        else:
            probs = math.exp(
                max_eos_token_prob
            )  # short job may have high eos prob. however, this value is too small to be considered.
            priority = self.get_gittins_index(seq_group, probs,
                                              decoding_length)
        return priority


class TFITTradeoff(Policy):

    def sigmoid(self, x, steepness=1, midpoint=0):
        if x < midpoint:
            return x
        return 1 / (1 + math.exp(x))
    
    def _get_running_priority(self, avg_priority_rate: float,
                              seq_group: SequenceGroup, pending_swapped_rate: float):
        # all_min_eos_token_pos = [seq.min_eos_rank for seq in seq_group.seqs_dict.values()] 
                                    #   seq_group: SequenceGroup):
        # min_eos_token_pos = min(
        #     (min(seq.get_eos_token_pos())
        #      for seq in seq_group.seqs_dict.values()),
        #     default=-1,
        # )

        all_eos_token_pos= []
        for seq in seq_group.seqs_dict.values():
            all_eos_token_pos.extend(seq.get_eos_token_pos())
        min_eos_token_pos = min(all_eos_token_pos, default=-1)
        if len(all_eos_token_pos) < 10:
            max_eos_token_pos= 32000
        else:
            max_eos_token_pos = np.mean(all_eos_token_pos[10:])
        # max_eos_token_pos = 32000
        # print(pending_swapped_rate)
        # max_eos_token_pos = 32000
        if min_eos_token_pos > 0:
            seq_group.priority_rate = (
                max_eos_token_pos - min_eos_token_pos) / max_eos_token_pos# 32,768, 50432
            # priority_rate_component = self.sigmoid(
                # seq_group.priority_rate, steepness=1, midpoint=0.9)
            seq_weight = (seq_group.seq_len / seq_group.max_length)
            # seq_weight_componenet = self.sigmoid(
                # seq_weight, steepness=1, midpoint=0.9)
            # priority =  (priority_rate_component + seq_weight_componenet) / 2
            # priority = priority_rate_component * seq_weight_componenet
            priority = ((seq_group.priority_rate) * seq_group.seq_len /
                            seq_group.max_length)
            
            
        else:
            # decode_length = sum(
                # seq.get_output_len() for seq in seq_group.seqs_dict.values())
            # priority = 1-(decode_length / seq_group.max_length)
            # seq_weight = (seq_group.seq_len / seq_group.max_length)
            # seq_weight_componenet = self.sigmoid(
                # seq_weight, steepness=1, midpoint=0.9)
            priority = seq_group.seq_len / seq_group.max_length
            # priority = seq_weight_componenet

        return priority

    def _get_waiting_priority(self, avg_priority_rate: float,
                              seq_group: SequenceGroup,
                              pending_swapped_rate: float):
        all_eos_token_pos= []
        for seq in seq_group.seqs_dict.values():
            all_eos_token_pos.extend(seq.get_eos_token_pos())
        min_eos_token_pos = min(all_eos_token_pos, default=-1)
        if len(all_eos_token_pos) < 10:
            max_eos_token_pos= 32000
        else:
            max_eos_token_pos = np.mean(all_eos_token_pos[10:])
        priority_rate = (max_eos_token_pos - min_eos_token_pos) / max_eos_token_pos
        if priority_rate > 0:
            # decode_length = sum(
            #     seq.get_output_len() for seq in seq_group.seqs_dict.values()
            # )
            # print(f"priority_rate: {priority_rate}, seq_group id is {seq_group.request_id}")
            seq_group.priority_rate = (
                32000 - priority_rate) / 32000  # 32,768, 50432
            priority = (
                seq_group.priority_rate*
                (seq_group.seq_len + seq_group.metrics.waiting_iter_nums) /
                seq_group.max_length)
        else:
            priority = avg_priority_rate*(
                (seq_group.seq_len + seq_group.metrics.waiting_iter_nums) /
                seq_group.max_length)
        # priority = (seq_group.seq_len - seq_group.metrics.waiting_iter_nums) / seq_group.max_length
        return priority

    def got_priority(
        self,
        avg_priority_rate: float,
        seq_group: SequenceGroup,
        pending_swapped_rate: float,
    ) -> float:
        if pending_swapped_rate != -1:
            priority = self._get_running_priority(avg_priority_rate, seq_group, pending_swapped_rate)
        else:
            priority = self._get_waiting_priority(avg_priority_rate, seq_group,
                                                  pending_swapped_rate)
        return priority

    def get_priority(
        self,
        now: float,
        seq_group: SequenceGroup,
    ) -> float:
        return now - seq_group.metrics.arrival_time


class Random(Policy):

    def get_priority(
        self,
        now: float,
        seq_group: SequenceGroup,
    ) -> float:
        return random.random()


class UncomputedTokensFirst(Policy):

    def get_priority(
        self,
        now: float,
        seq_group: SequenceGroup,
    ) -> float:
        return seq_group.get_num_uncomputed_tokens()


class WaitingTimeFirst(Policy):

    def get_priority(
        self,
        now: float,
        seq_group: SequenceGroup,
    ) -> float:
        return seq_group.metrics.waiting_iter_nums


class ShortRemainJobFirst(Policy):

    def get_priority(
        self,
        now: float,
        seq_group: SequenceGroup,
    ) -> float:
        total_output_lens = sum(
            [seq.get_output_len() for seq in seq_group.get_seqs()])
        priority = -(seq_group.max_length - total_output_lens)
        return priority


class ShortJobFirst(Policy):

    def get_priority(
        self,
        now: float,
        seq_group: SequenceGroup,
    ) -> float:
        priority = -seq_group.max_length
        return priority


class LeastAttainedSvr(Policy):

    def get_priority(
        self,
        now: float,
        seq_group: SequenceGroup,
    ) -> float:
        decode_length = sum(seq.get_output_len()
                            for seq in seq_group.seqs_dict.values())
        # priority = -seq_group.seq_len
        priority = -decode_length
        return priority


class LongJobFirst(Policy):

    def get_priority(
        self,
        now: float,
        seq_group: SequenceGroup,
    ) -> float:
        tokens = 0
        waiting_iter_nums = seq_group.metrics.waiting_iter_nums
        for seq_id, seq in seq_group.seqs_dict.items():
            tokens += seq.get_len()
        priority = tokens - waiting_iter_nums * waiting_iter_nums
        return priority


class PolicyFactory:
    _POLICY_REGISTRY = {
        "fcfs": FCFS,
        "utf": UncomputedTokensFirst,
        "random": Random,
        "wtf": WaitingTimeFirst,
        "sjf": ShortJobFirst,
        "srjf": ShortRemainJobFirst,
        "las": LeastAttainedSvr,
        "ljf": LongJobFirst,
        "infer": TFTLatencyTrade,
        "sjmlfq": SkipJoinMLFQ,
        "inferpreempt": TFTLatencyTrade,
        "tfittradeoff": TFITTradeoff,
    }

    @classmethod
    def get_policy(cls, policy_name: str, **kwargs) -> Policy:
        return cls._POLICY_REGISTRY[policy_name](**kwargs)
