#!/bin/bash
# Start an interactive Bluesky session for 6-ID-B.
# Usage: ./start_bluesky.sh

CONDA_ENV="6idb-bits"

# Activate conda
CONDA_BASE=$(conda info --base 2>/dev/null)
if [ -z "${CONDA_BASE}" ]; then
    echo "ERROR: conda not found. Make sure conda is initialized in your shell."
    exit 1
fi

source "${CONDA_BASE}/etc/profile.d/conda.sh"
conda activate "${CONDA_ENV}"

if [ "${CONDA_DEFAULT_ENV}" != "${CONDA_ENV}" ]; then
    echo "ERROR: failed to activate conda environment '${CONDA_ENV}'."
    exit 1
fi

echo "Starting Bluesky session for 6-ID-B (env: ${CONDA_ENV}) ..."
ipython -i -c "from id6_b.startup import *"
