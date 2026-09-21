#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)


# Keep these values fixed to avoid dual-source configuration during debugging.
# Usage: from the repo root, run `docker build -f prepare_docker_env/DockerfileCP9201V2 -t cp9201v2 .` once, then run `bash prepare_docker_env/run_batch_reduce_cp9201_v2.sh [command ...]`.

# List all containers using the cp9201v2 image: docker ps -a --filter ancestor=cp9201v2

IMAGE_NAME="${IMAGE_NAME:-cp9201v2}"
PROJECT_MOUNT="${PROJECT_MOUNT:-/workspace/E2WR}"
RUNTIME_HOST_ROOT="${RUNTIME_HOST_ROOT:?set RUNTIME_HOST_ROOT to the host directory holding the wasm runtimes}"
RUNTIME_MOUNT="${RUNTIME_MOUNT:-/workspace/ReduceRuntimes}"
OUTPUT_HOST_ROOT="${OUTPUT_HOST_ROOT:?set OUTPUT_HOST_ROOT to the host directory for reduction outputs}"
OUTPUT_MOUNT="${OUTPUT_MOUNT:-/workspace/E2WR_outputs}"

if [[ ! -d "$RUNTIME_HOST_ROOT" ]]; then
    echo "Runtime directory not found: $RUNTIME_HOST_ROOT" >&2
    exit 1
fi

if [[ ! -d "$OUTPUT_HOST_ROOT" ]]; then
    echo "Output directory not found: $OUTPUT_HOST_ROOT" >&2
    exit 1
fi

if [[ $# -eq 0 ]]; then
    set -- python E2WR/script_run_reduce_v2.py --help
fi

    # --pids-limit=64 \
docker run --rm -it \
    --memory=32g \
    --memory-swap=32g \
    -v "$REPO_ROOT:$PROJECT_MOUNT" \
    -v "$RUNTIME_HOST_ROOT:$RUNTIME_MOUNT:ro" \
    -v "$OUTPUT_HOST_ROOT:$OUTPUT_MOUNT" \
    -w "$PROJECT_MOUNT" \
    -e CP9201_PATH_PROFILE=container \
    -e CP9201_RUNTIME_ROOT="$RUNTIME_MOUNT" \
    -e CP9201_REDUCER_ROOT="$PROJECT_MOUNT/E2WR" \
    -e ORACLE_DATA_DIR="$OUTPUT_MOUNT/oracle_data_v3" \
    -e VALID_DATA_DIR="$OUTPUT_MOUNT/valid_data_v3" \
    -e CP9201_BENCHMARK_DIR="$PROJECT_MOUNT/benchmark" \
    "$IMAGE_NAME" \
    "$@"
