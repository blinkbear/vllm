# 读取当前计数器的值
COUNTER_FILE=".counter.txt"
if [ -f "$COUNTER_FILE" ]; then
  COUNTER=$(cat $COUNTER_FILE)
else
  COUNTER=0
fi
# 自增计数器
COUNTER=$((COUNTER + 1))
# 将新的计数器值写回文件
echo $COUNTER >$COUNTER_FILE

# start vllm server
model_name="meta-llama/Llama-2-13b-chat-hf"
dataset_name="sharegpt"
dataset_path="/root/v1/vllm/dataset/ShareGPT_V3_unfiltered_cleaned_split.json"
result_dir="/root/v1/vllm/benchmarks/result"
# scheduler_policy=(fcfs)
# swap_policies=(full)
# scheduler_policy=(infer)
# swap_policies=(partial)
declare -a scheduler_swap_policies
# scheduler_swap_policies[0]="fcfs full"
# scheduler_swap_policies[1]="tfittradeoff full"
scheduler_swap_policies[2]="tfittradeoff partial"
# scheduler_swap_policies[2]="sjf full"
# scheduler_swap_policies[3]="sjmlfq full"
# scheduler_swap_policies[3]="infer partial"
# scheduler_swap_policies[4]="inferpreempt full"
# scheduler_swap_policies[5]="sjmlfq full"fish

preemption_mode="swap"
gpu_memory_utilization=0.5 # 0.5, 0.7, 0.9
max_num_seqs=128
swap_space=64
max_tokens=2048
iter_theshold=15

# request_rates[0]=0.5
# request_rates[1]=1.0
request_rates[2]=2.0
# request_rates[3]=5.0
# request_rates[4]=10.0
# request_rates[5]=20.0

# request_rates=(2.0)
swap_out_partial_rates=(0.5)
waiting_iter_base=(0.1)
gpu_devices=1
for i in {0..0}; do
  for waiting_iter in "${waiting_iter_base[@]}"; do
    for swap_out_partial_rate in "${swap_out_partial_rates[@]}"; do
      for request_rate in "${request_rates[@]}"; do
        for scheduler_swap_policy in "${scheduler_swap_policies[@]}"; do
          element=(${scheduler_swap_policy})
          policy=${element[0]}
          swap_policy=${element[1]}
          CUDA_VISIBLE_DEVICES=$gpu_devices taskset -c 20-21 python3 -m vllm.entrypoints.openai.api_server \
            --model $model_name --swap-space $swap_space --preemption-mode $preemption_mode --scheduler-policy $policy \
            --enable-chunked-prefill --max-num-batched-tokens $max_tokens --iter-threshold $iter_theshold --max-num-seqs $max_num_seqs --swap-out-tokens-policy $swap_policy --swap-out-partial-rate $swap_out_partial_rate --execution-budget $iter_theshold \
            --gpu-memory-utilization $gpu_memory_utilization --waiting-iter-base $waiting_iter --disable-log-requests >api_server_${policy}_${swap_policy}.log 2>&1 &
          pid=$!

          # run benchmark and save the output to benchmark.log
          python3 benchmark_serving.py --execution-counter $COUNTER --dataset-path $dataset_path \
            --dataset-name $dataset_name --request-rate $request_rate \
            --num-prompts 500 --request-duration 600 --sharegpt-output-len 2000 --model $model_name --scheduler-policy $policy \
            --save-result --result-dir $result_dir \
            --metadata swap_space=$swap_space preemption_mode=$preemption_mode \
            scheduler_policy=$policy gpu_memory_utilization=$gpu_memory_utilization \
            max_num_seqs=$max_num_seqs max_tokens=$max_tokens swap_policy=$swap_policy \
            iter_theshold=$iter_theshold swap_out_partial_rate=$swap_out_partial_rate waiting_iter_base=$waiting_iter >>benchmark-${policy}.log 2>&1
          sleep 5
          kill $pid
          python3 parse_log.py --policy $policy --swap-policy $swap_policy --result-dir $result_dir \
            --execution-counter $COUNTER --request-rate $request_rate \
            --swap-out-partial-rate $swap_out_partial_rate --model $model_name
          # sleep 10
        done
      done
    done
  done
done