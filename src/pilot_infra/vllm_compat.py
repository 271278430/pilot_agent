"""Owner image-build compatibility for vLLM 0.8.5's numeric-only NVML helper."""


def uuid_compatible_source(source):
    original = "        return int(physical_device_id)"
    if "def device_id_to_physical_device_id(device_id: int) -> int:" not in source or source.count(original) != 1:
        raise ValueError("vLLM NVML helper differs from the admitted 0.8.5 recipe")
    replacement = """        # Pilot image compatibility: preserve UUID isolation and query the
        # matching NVML device rather than guessing a physical index.
        if physical_device_id.startswith("GPU-"):
            pynvml.nvmlInit()
            try:
                handle = pynvml.nvmlDeviceGetHandleByUUID(physical_device_id)
                return pynvml.nvmlDeviceGetIndex(handle)
            finally:
                pynvml.nvmlShutdown()
        return int(physical_device_id)"""
    return source.replace(original, replacement)
