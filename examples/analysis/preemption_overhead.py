import argparse
from typing import Tuple, Dict
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

pretty.install()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# test preemption overhead for same prompt.
def get_requests() -> Dict[int, Tuple[str, SamplingParams, int]]:
    init_seq = {}
    saved_seq = Utils.load_seq_from_file(BASE_DIR, "seq_data", "selected_seq.json")
    for p_len in saved_seq:
        prompt_len = int(p_len)
        prompt = saved_seq[p_len]
        init_seq[prompt_len] = (
            prompt,
            SamplingParams(
                temperature=0.0,
                logprobs=1,
                min_tokens=300,
                max_tokens=301,
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
    if prefill_mode == "vertical":
        # create a batch whose size is $init_prompt_nums$ and each seq length is 1
        selected_seqs = [seqs[16]] * init_prompt_nums
    elif prefill_mode == "horizonal":
        # create a batch whose size is 1 and each seq length is init_prompt_nums
        selected_seqs = [seqs[init_prompt_nums]]
    for i in range(len(selected_seqs)):
        prompts_queue.put(selected_seqs[i])



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
    default_preemption_mode: str = "recompute",
    strategy: str = "full",
    prefill_mode: str = "vertical",
):
    """Main function that sets up and runs the prompt processing."""
    parser = argparse.ArgumentParser(
        description="Demo on using the LLMEngine class directly"
    )

    parser = EngineArgs.add_cli_args(parser)
    args: argparse.Namespace = parser.parse_args()
    args.model = "meta-llama/Llama-2-13b-hf"
    args.max_num_seqs = batch_size
    args.scheduler_policy = policy
    args.default_preemption_mode = default_preemption_mode
    # args.gpu_memory_utilization = 0.5
    if enable_chunk_prefill:
        args.enable_chunked_prefill = True
        args.max_num_batched_tokens = max_token_num
    seqs = get_requests()
    try:
        engine = initialize_engine(args)
    except Exception as e:
        print(e)
    add_new_request_notice = Queue()
    print(f"start preemption: {default_preemption_mode}")
    for repeat_time in range(5):
        prompts_queue = Queue()
        try:
            create_init_prompts(
                seqs,
                prompts_queue,
                batch_size,
                prefill_mode,
            )
            insert_new_request=False
            with ThreadPoolExecutor(max_workers=2) as executor:
                executor.submit(
                    Utils.process_requests,
                    engine=engine,
                    prompts_queue=prompts_queue,
                    add_new_request_notice=add_new_request_notice,
                    strategy=strategy,
                    result_queue=result_queue,
                    batch_size=batch_size,
                    enable_chunk_prefill=enable_chunk_prefill,
                    policy=policy,
                    repeat_time=repeat_time,
                    max_token_num=max_token_num,
                    random_seed=10,
                    prefill_mode=prefill_mode,
                    insert_new_request=insert_new_request,
                    insert_new_request_round=3,
                    preemption_mode=args.default_preemption_mode
                )
                executor.shutdown(wait=True)
        except Exception as e:
            print(e)


def skip_combination(df, batch_size, policy="fcfs", random_seed=10):
    if df.shape[0] == 0:
        return False
    tmp = df[
        (df["batch_size"] == batch_size)
        & (df["policy"] == policy)
        & (df["random_seed"] == random_seed)
    ]
    if tmp.shape[0] == 0:
        return False
    return True


if __name__ == "__main__":
    test_type = "preemption_overhead_swapout"
    rerun = True
    with mp.Manager() as manager:
        result_queue = manager.Queue()
        max_token_nums = [1912]
        batch_sizes = [512]
        total_iter_result, total_request_result = Utils.load_tmp_result(
            test_type, BASE_DIR
        )
        enable_chunk_prefill = True
        # default_preemption_mode = "swap"
        preemption_modes = ["swap", "recompute"]
        default_policy = "fcfs"
        strategies = ["hybrid"]
        # If prefill mode is horizonal, the sequences length is equals to the token nums, otherwise, the batch size equals to the token nums  # noqa: E501
        prefill_modes = ["vertical"]
        for preemption_mode in preemption_modes:
            for strategy in strategies:
                for prefill_mode in prefill_modes:
                    for batch_size in batch_sizes:
                        for max_token_num in max_token_nums:
                            try:
                                if (
                                    skip_combination(
                                        total_iter_result,
                                        batch_size,
                                    )
                                    and not rerun
                                    and (
                                        prefill_mode == "horizonal"
                                        and strategy == "hybrid"
                                    )
                                ):
                                    continue
                                with ProcessPoolExecutor(max_workers=2) as executor:
                                    executor.submit(
                                        main,
                                        max_token_num=max_token_num,
                                        batch_size=batch_size,
                                        result_queue=result_queue,
                                        enable_chunk_prefill=enable_chunk_prefill,
                                        policy=default_policy,
                                        default_preemption_mode=preemption_mode,
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
                                        [total_iter_result, iter_result]
                                    )
                                    total_request_result = pd.concat(
                                        [total_request_result, request_result]
                                    )
                                if len(total_iter_result) > 0:
                                    Utils.save_tmp_result(
                                        total_iter_result,
                                        total_request_result,
                                        test_type,
                                        BASE_DIR,
                                    )
                                    print("save tmp results successfully!")
                            except Exception as e:
                                print(e)
        if len(total_iter_result) > 0:
            Utils.save_result(
                total_iter_result,
                total_request_result,
                enable_chunk_prefill,
                test_type,
                rerun,
                BASE_DIR,
            )
