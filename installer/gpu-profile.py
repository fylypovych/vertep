import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.gpu_profiles import gpu_profile


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve a deterministic Vertep GPU compatibility profile")
    parser.add_argument("--name", required=True)
    parser.add_argument("--vram-mb", type=int, required=True)
    parser.add_argument("--compute-capability", default="")
    parser.add_argument("--format", choices=["json", "tsv", "env"], default="json")
    args = parser.parse_args()
    profile = gpu_profile(args.name, args.vram_mb, args.compute_capability)
    if args.format == "tsv":
        print("\t".join(str(profile[key]) for key in
                        ("architecture", "profile_id", "torch_index_url", "torch_packages", "comfyui_args")))
    elif args.format == "env":
        print(f"GPU_PROFILE={profile['profile_id']}")
        print(f"GPU_ARCHITECTURE={profile['architecture']}")
        print(f"COMFYUI_ARGS={profile['comfyui_args']}")
    else:
        print(json.dumps(profile, ensure_ascii=False))


if __name__ == "__main__":
    main()
