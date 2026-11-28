#!/usr/bin/env bash
# 在这里修改实验参数，也可用 MODEL=... ROUNDS=... bash scripts/run_study.sh 覆盖。
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 模型与进化：默认两组依次运行，不并发占用服务。
MODEL="${MODEL:-DeepSeek-V4-Flash}"
TARGET_MODEL="${TARGET_MODEL:-$MODEL}"
WORKER_MODEL="${WORKER_MODEL:-$MODEL}"
ROUNDS="${ROUNDS:-4}"
ALGORITHMS="${ALGORITHMS:-meta_harness ca_rsi}"
SEEDS="${SEEDS:-43}"                         # 多种子例子："43 44 45"

# 数据规模：固定数据版本和评分方式，沿用 CONFIG；测试集不参与选择。
TRAIN_SIZE="${TRAIN_SIZE:-8}"
DEV_SIZE="${DEV_SIZE:-16}"                   # CA-RSI 按轮分配新题，必须 >= ROUNDS。
TEST_SIZE="${TEST_SIZE:-16}"

# CA-RSI 选择：k 个修改文件最多评估 2^k 个组合；只对 CA-RSI 生效。
MAX_EDIT_UNITS="${MAX_EDIT_UNITS:-3}"        # 1~4；超限候选整轮跳过。
MAX_TASK_REGRESSIONS="${MAX_TASK_REGRESSIONS:-0}" # 新题与旧题检查允许退步的题数。

# 单次 Target / Worker 的交互预算；不是整场实验的累计 token 预算。
TARGET_MAX_TURNS="${TARGET_MAX_TURNS:-6}"
WORKER_MAX_TURNS="${WORKER_MAX_TURNS:-30}"
TARGET_TIMEOUT="${TARGET_TIMEOUT:-300}"     # 秒
WORKER_TIMEOUT="${WORKER_TIMEOUT:-600}"     # 秒
TEMPERATURE="${TEMPERATURE:-1}"
# 思考模式、客户端输出 token 不设上限等沿用 CONFIG 中的设置。

CONFIG="${CONFIG:-$PROJECT_ROOT/configs/hotpotqa_pilot.yaml}"
CONTAINER_IMAGE="${CONTAINER_IMAGE:-ca-rsi-runtime:local}"
OUTPUT_DIR="${OUTPUT_DIR:-$PROJECT_ROOT/runs/study}"
VISUALIZATION_OUTPUT="${VISUALIZATION_OUTPUT:-$PROJECT_ROOT/results/evolution-live.html}"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_ROOT/.venv/bin/python}"

if [[ $# -gt 1 || ( $# -eq 1 && "$1" != --dry-run ) ]]; then
    echo '用法：bash scripts/run_study.sh [--dry-run]；参数在脚本开头或同名环境变量中设置。' >&2
    exit 2
fi
if [[ ! -x "$PYTHON_BIN" ]]; then
    echo '缺少 Python 环境，请先运行 sh scripts/setup.sh。' >&2
    exit 2
fi
cd "$PROJECT_ROOT"
read -r -a study_algorithms <<< "$ALGORITHMS"
read -r -a study_seeds <<< "$SEEDS"
if [[ ${#study_algorithms[@]} -eq 0 || ${#study_seeds[@]} -eq 0 ]]; then
    echo 'ALGORITHMS 和 SEEDS 不能为空。' >&2
    exit 2
fi
for algorithm in "${study_algorithms[@]}"; do
    case "$algorithm" in meta_harness|ca_rsi|recursive_ca_rsi) ;; *) echo "不支持的算法：$algorithm" >&2; exit 2 ;; esac
done
for seed in "${study_seeds[@]}"; do
    if [[ ! "$seed" =~ ^[0-9]+$ ]]; then echo "无效种子：$seed" >&2; exit 2; fi
done

study_args=(
    --config "$CONFIG" --model "$MODEL"
    --target-model "$TARGET_MODEL" --worker-model "$WORKER_MODEL"
    --rounds "$ROUNDS" --max-edit-units "$MAX_EDIT_UNITS"
    --max-task-regressions "$MAX_TASK_REGRESSIONS"
    --train-size "$TRAIN_SIZE" --dev-size "$DEV_SIZE" --test-size "$TEST_SIZE"
    --target-max-turns "$TARGET_MAX_TURNS" --worker-max-turns "$WORKER_MAX_TURNS"
    --target-timeout "$TARGET_TIMEOUT" --worker-timeout "$WORKER_TIMEOUT"
    --temperature "$TEMPERATURE" --container-image "$CONTAINER_IMAGE"
    --visualization-output "$VISUALIZATION_OUTPUT"
)

# 先检查所有组合的参数；--dry-run 不检查 API、不调用模型、不写运行目录。
for seed in "${study_seeds[@]}"; do
    for algorithm in "${study_algorithms[@]}"; do
        command_args=("${study_args[@]}" --algorithm "$algorithm" --seed "$seed"
            --output-dir "$OUTPUT_DIR/$algorithm/seed-$seed")
        if [[ "${1:-}" == --dry-run ]]; then
            "$PYTHON_BIN" scripts/run_experiment.py "${command_args[@]}" --dry-run
        else
            "$PYTHON_BIN" scripts/run_experiment.py "${command_args[@]}" --dry-run > /dev/null
        fi
    done
done
if [[ "${1:-}" == --dry-run ]]; then exit 0; fi

failures=0
for seed in "${study_seeds[@]}"; do
    for algorithm in "${study_algorithms[@]}"; do
        echo "运行 ${algorithm}，seed=${seed}，轮数=${ROUNDS}，Target=${TARGET_MODEL}，Worker=${WORKER_MODEL}"
        if ! "$PYTHON_BIN" scripts/run_experiment.py "${study_args[@]}" \
            --algorithm "$algorithm" --seed "$seed" --output-dir "$OUTPUT_DIR/$algorithm/seed-$seed"; then
            failures=$((failures + 1))
            echo "$algorithm / seed=$seed 失败；继续后续组合，已完成的结果保留。" >&2
        fi
    done
done
echo "可视化：$VISUALIZATION_OUTPUT"
if [[ $failures -gt 0 ]]; then echo "$failures 组未成功完成，见上方错误。" >&2; exit 1; fi
