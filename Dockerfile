# The vLLM Dockerfile is used to construct vLLM image that can be directly used
# to run the OpenAI compatible server.

# Please update any changes made here to
# docs/source/dev/dockerfile/dockerfile.rst and
# docs/source/assets/dev/dockerfile-stages-dependency.png

#################### BASE BUILD IMAGE ####################
# prepare basic build environment
FROM nvidia/cuda:12.4.1-devel-ubuntu22.04 AS dev

RUN apt-get update -y \
    && apt-get install -y python3-pip git

# Workaround for https://github.com/openai/triton/issues/2507 and
# https://github.com/pytorch/pytorch/issues/107960 -- hopefully
# this won't be needed for future versions of this docker image
# or future versions of triton.
RUN ldconfig /usr/local/cuda-12.4/compat/

WORKDIR /workspace

# install build and runtime dependencies
COPY requirements-common.txt requirements-common.txt
COPY requirements-cuda.txt requirements-cuda.txt
RUN pip install -r requirements-cuda.txt

# install development dependencies
COPY requirements-dev.txt requirements-dev.txt
RUN pip install -r requirements-dev.txt

# cuda arch list used by torch
# can be useful for both `dev` and `test`
# explicitly set the list to avoid issues with torch 2.2
# see https://github.com/pytorch/pytorch/pull/123243
ARG torch_cuda_arch_list='7.0 7.5 8.0 8.6 8.9 9.0+PTX'
ENV TORCH_CUDA_ARCH_LIST=${torch_cuda_arch_list}


COPY requirements-build.txt requirements-build.txt
RUN pip install -r requirements-build.txt
RUN apt-get update -y && apt-get install -y ccache

COPY csrc csrc
COPY setup.py setup.py
COPY cmake cmake
COPY CMakeLists.txt CMakeLists.txt
COPY requirements-common.txt requirements-common.txt
COPY requirements-cuda.txt requirements-cuda.txt
COPY pyproject.toml pyproject.toml
COPY vllm vllm

# max jobs used by Ninja to build extensions
ARG max_jobs=10
ENV MAX_JOBS=${max_jobs}
# number of threads used by nvcc
ARG nvcc_threads=8
ENV NVCC_THREADS=$nvcc_threads
# make sure punica kernels are built (for LoRA)
ENV VLLM_INSTALL_PUNICA_KERNELS=1

RUN python3 setup.py bdist_wheel --dist-dir=dist

# check the size of the wheel, we cannot upload wheels larger than 100MB
COPY .buildkite/check-wheel-size.py check-wheel-size.py
RUN python3 check-wheel-size.py dist


RUN apt-get update -y \
    && apt-get install -y vim 
    
RUN pip install dist/*.whl --verbose

