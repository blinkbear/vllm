import argparse
from typing import List, Tuple, Dict
from queue import Queue
from vllm import EngineArgs, LLMEngine, SamplingParams
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import pandas as pd
import multiprocessing as mp
from multiprocessing import Queue as MQueue
import os
from utils import Utils
from rich import print
from rich import pretty
import random
import traceback

pretty.install()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def get_requests() -> Dict[int, Tuple[str, SamplingParams, int]]:
    init_seq = {}
    saved_seq = Utils.load_seq_from_file(BASE_DIR, "seq_data",
                                         "selected_seq.json")
    for p_len in saved_seq:
        prompt_len = int(p_len)
        prompt = saved_seq[p_len]
        init_seq[prompt_len] = (
            prompt,
            SamplingParams(
                temperature=0.0,
                logprobs=1,
                min_tokens=1,
                max_tokens=600,
            ),
            prompt_len,
        )

    return init_seq


def create_init_prompts(
    seqs: Dict[int, Tuple[str, SamplingParams, int]],
    prompts_queue: Queue,
    init_prompt_nums: int,
    prefill_mode: str,
):
    random.seed(1)
    all_prompt_nums = list(seqs.keys())
    if prefill_mode == "vertical":
        # create a batch whose size is $init_prompt_nums$ and each seq length is 1
        selected_prompt_nums = random.choices(all_prompt_nums,
                                              k=init_prompt_nums)
        selected_seqs = [seqs[i] for i in selected_prompt_nums]
    elif prefill_mode == "horizonal":
        # create a batch whose size is 1 and each seq length is init_prompt_nums
        selected_seqs = [seqs[init_prompt_nums]]
    for i in range(len(selected_seqs)):
        prompts_queue.put(selected_seqs[i])


def add_new_request(
    requests: List[Tuple[str, SamplingParams, int]],
    prompts_queue: Queue,
    add_new_request_notice: Queue,
    request_nums: int,
):
    """Add a new request to the queue, every 1 seconds."""

    add_new_request_notice.get()
    requests = requests * request_nums
    for i in range(request_nums):
        prompts_queue.put(requests[i])


def initialize_engine(args: argparse.Namespace) -> LLMEngine:
    """Initialize the LLMEngine from the command line arguments."""
    engine_args = EngineArgs.from_cli_args(args)
    return LLMEngine.from_engine_args(engine_args)


def main(
    max_token_num: int,
    batch_size: int,
    result_queue: MQueue,
    enable_chunk_prefill: bool = False,
    policy: str = "fcfs",
    preemption_mode: str = "swap",
    strategy: str = "full",
    prefill_mode: str = "vertical",
):
    """Main function that sets up and runs the prompt processing."""
    parser = argparse.ArgumentParser(
        description="Demo on using the LLMEngine class directly")

    parser = EngineArgs.add_cli_args(parser)
    args: argparse.Namespace = parser.parse_args()
    args.model = "meta-llama/Llama-2-13b-chat-hf"
    args.swap_space = 16
    args.max_num_seqs = batch_size
    args.scheduler_policy = policy
    args.default_preemption_mode = preemption_mode
    args.enable_chunked_prefill = True
    args.max_num_batched_tokens = max_token_num
    try:
        seqs = get_requests()
        engine = initialize_engine(args)
    except Exception as e:
        traceback.print_exc()
        print(e)
    add_new_request_notice = Queue()
    print(
        f"start strategy: {strategy}, prefill_mode: {prefill_mode}, policy is {policy}"
    )
    for repeat_time in range(1):
        prompts_queue = Queue()
        updated_token_num = int(batch_size)
        insert_new_request = False
        create_init_prompts(
            seqs,
            prompts_queue,
            updated_token_num,
            prefill_mode,
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            executor.submit(
                Utils.process_requests,
                engine=engine,
                prompts_queue=prompts_queue,
                add_new_request_notice=add_new_request_notice,
                strategy=strategy,
                result_queue=result_queue,
                batch_size=updated_token_num,
                enable_chunk_prefill=enable_chunk_prefill,
                policy=policy,
                repeat_time=repeat_time,
                max_token_num=max_token_num,
                random_seed=10,
                prefill_mode=prefill_mode,
                insert_new_request=insert_new_request,
                preemption_mode=preemption_mode,
                insert_new_request_round=3,
            )
            executor.shutdown(wait=True)


def skip_combination(df, batch_size, policy="fcfs", random_seed=10):
    if df.shape[0] == 0:
        return False
    tmp = df[(df["batch_size"] == batch_size)
             & (df["policy"] == policy)
             & (df["random_seed"] == random_seed)]
    if tmp.shape[0] == 0:
        return False
    return True


if __name__ == "__main__":
    test_type = "infer_schedule_policy_test"
    rerun = True
    with mp.Manager() as manager:
        result_queue = manager.Queue()
        max_token_nums = [1912]
        batch_sizes = [16]
        total_iter_result, total_request_result = Utils.load_tmp_result(
            test_type, BASE_DIR)
        enable_chunk_prefill = True
        preemption_mode = "swap"
        policies = ["fcfs"]
        strategies = ["full"]
        # If prefill mode is horizonal, the sequences length is equals to the token nums, otherwise, the batch size equals to the token nums  # noqa: E501
        prefill_modes = ["vertical"]
        for strategy in strategies:
            for batch_size in batch_sizes:
                for prefill_mode in prefill_modes:
                    for policy in policies:
                        for max_token_num in max_token_nums:
                            try:
                                if (skip_combination(
                                        total_iter_result,
                                        batch_size,
                                ) and not rerun
                                        and (prefill_mode == "horizonal"
                                             and strategy == "hybrid")):
                                    print("skip this combination")
                                    continue
                                with ProcessPoolExecutor(
                                        max_workers=2) as executor:
                                    executor.submit(
                                        main,
                                        max_token_num=max_token_num,
                                        batch_size=batch_size,
                                        result_queue=result_queue,
                                        enable_chunk_prefill=
                                        enable_chunk_prefill,
                                        policy=policy,
                                        preemption_mode=preemption_mode,
                                        strategy=strategy,
                                        prefill_mode=prefill_mode,
                                    )
                                    executor.shutdown(wait=True)
                                while not result_queue.empty():
                                    item = result_queue.get()
                                    iter_result, request_result = (
                                        item[0],
                                        item[1],
                                    )
                                    total_iter_result = pd.concat(
                                        [total_iter_result, iter_result])
                                    total_request_result = pd.concat(
                                        [total_request_result, request_result])
                            #     if len(total_iter_result) > 0:
                            #         Utils.save_tmp_result(
                            #             total_iter_result,
                            #             total_request_result,
                            #             test_type,
                            #             BASE_DIR,
                            #         )
                            except Exception as e:
                                traceback.print_exc()
                                print(e)

        #     Utils.save_result(
        #         total_iter_result,
        #         total_request_result,
        #         enable_chunk_prefill,
        #         test_type,
        #         rerun,
        #         BASE_DIR,
        #   )
