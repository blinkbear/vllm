import pickle
from typing import List, Optional, Tuple

from vllm.config import ParallelConfig
from vllm.logger import init_logger
from vllm.utils import get_ip, is_hip, is_xpu
from vllm.worker.worker_base import WorkerWrapperBase

logger = init_logger(__name__)

try:
    import ray

    class RayWorkerWrapper(WorkerWrapperBase):
        """Ray wrapper for vllm.worker.Worker, allowing Worker to be
        lazliy initialized after Ray sets CUDA_VISIBLE_DEVICES."""

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            # Since the compiled DAG runs a main execution
            # in a different thread that calls cuda.set_device.
            # The flag indicates is set_device is called on
            # that thread.
            self.compiled_dag_cuda_device_set = False

        def get_node_ip(self) -> str:
            return get_ip()

        def get_node_and_gpu_ids(self) -> Tuple[str, List[int]]:
            node_id = ray.get_runtime_context().get_node_id()
            gpu_ids = ray.get_gpu_ids()
            return node_id, gpu_ids

        def execute_model_compiled_dag_remote(self, ignored):
            """Used only when compiled DAG is enabled."""
            import torch
            if not self.compiled_dag_cuda_device_set:
                torch.cuda.set_device(self.worker.device)
                self.compiled_dag_cuda_device_set = True

            output = self.worker.execute_model()
            output = pickle.dumps(output)
            return output

    ray_import_err = None

except ImportError as e:
    ray = None  # type: ignore
    ray_import_err = e
    RayWorkerWrapper = None  # type: ignore


def ray_is_available() -> bool:
    """Returns True if Ray is available."""
    return ray is not None


def assert_ray_available():
    """Raise an exception if Ray is not available."""
    if ray is None:
        raise ValueError("Failed to import Ray, please install Ray with "
                         "`pip install ray`.") from ray_import_err


def initialize_ray_cluster(
    parallel_config: ParallelConfig,
    ray_address: Optional[str] = None,
):
    """Initialize the distributed cluster with Ray.

    it will connect to the Ray cluster and create a placement group
    for the workers, which includes the specification of the resources
    for each distributed worker.

    Args:
        parallel_config: The configurations for parallel execution.
        ray_address: The address of the Ray cluster. If None, uses
            the default Ray cluster address.
    """
    assert_ray_available()
    # Connect to a ray cluster.
    if is_hip() or is_xpu():
        ray.init(address=ray_address,
                 ignore_reinit_error=True,
                 num_gpus=parallel_config.world_size)
    else:
        ray.init(address=ray_address, ignore_reinit_error=True)

    if parallel_config.placement_group:
        # Placement group is already set.
        return

    # Create placement group for worker processes
    current_placement_group = ray.util.get_current_placement_group()
    if current_placement_group:
        # We are in a placement group
        bundles = current_placement_group.bundle_specs
        # Verify that we can use the placement group.
        gpu_bundles = 0
        for bundle in bundles:
            bundle_gpus = bundle.get("GPU", 0)
            if bundle_gpus > 1:
                raise ValueError(
                    "Placement group bundle cannot have more than 1 GPU.")
            if bundle_gpus:
                gpu_bundles += 1
        if parallel_config.world_size > gpu_bundles:
            raise ValueError(
                "The number of required GPUs exceeds the total number of "
                "available GPUs in the placement group.")
    else:
        num_gpus_in_cluster = ray.cluster_resources().get("GPU", 0)
        if parallel_config.world_size > num_gpus_in_cluster:
            raise ValueError(
                "The number of required GPUs exceeds the total number of "
                "available GPUs in the cluster.")
        # Create a new placement group
        placement_group_specs = ([{"GPU": 1}] * parallel_config.world_size)
        current_placement_group = ray.util.placement_group(
            placement_group_specs)
        # Wait until PG is ready - this will block until all
        # requested resources are available, and will timeout
        # if they cannot be provisioned.
        ray.get(current_placement_group.ready(), timeout=1800)

    # Set the placement group in the parallel config
    parallel_config.placement_group = current_placement_group
