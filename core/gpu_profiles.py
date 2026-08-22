import re


def gpu_profile(name: str, vram_mb: int, compute_capability: str) -> dict:
    normalized_name = name.strip()
    match = re.match(r"^(\d+)(?:\.(\d+))?", compute_capability.strip())
    major = int(match.group(1)) if match else 0
    minor = int(match.group(2) or 0) if match else 0
    capability = major + minor / 10
    is_gtx_1660 = bool(re.search(r"(?:geforce\s+)?gtx\s*1660(?:\s*(?:super|ti))?", normalized_name,
                                 flags=re.IGNORECASE))

    if is_gtx_1660 or capability == 7.5:
        architecture = "Turing"
    elif 6 <= capability < 7:
        architecture = "Pascal"
    elif 8 <= capability < 9:
        architecture = "Ampere/Ada"
    elif capability >= 9:
        architecture = "Hopper or newer"
    else:
        architecture = "unknown"

    if is_gtx_1660:
        profile_id = "gtx1660-6gb"
    elif architecture == "Pascal":
        profile_id = "pascal"
    elif architecture == "Turing":
        profile_id = "turing"
    else:
        profile_id = "modern"

    if architecture == "Pascal":
        torch_index = "https://download.pytorch.org/whl/cu121"
        torch_packages = "torch==2.5.1 torchvision==0.20.1 torchaudio==2.5.1"
    else:
        torch_index = "https://download.pytorch.org/whl/cu124"
        torch_packages = "torch==2.6.0 torchvision==0.21.0 torchaudio==2.6.0"
    return {
        "profile_id": profile_id,
        "gpu_name": normalized_name or "unknown",
        "architecture": architecture,
        "compute_capability": compute_capability.strip() or "unknown",
        "vram_mb": vram_mb,
        "supported": capability >= 6 or is_gtx_1660,
        "torch_packages": torch_packages,
        "torch_index_url": torch_index,
        "comfyui_args": "--lowvram" if 0 < vram_mb <= 8192 else "",
        "recommended_tasks": ["image"],
    }
