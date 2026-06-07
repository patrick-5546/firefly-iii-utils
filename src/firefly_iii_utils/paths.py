from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIGS_DIR = REPO_ROOT / "configs"
ACCOUNT_MAPPINGS_PATH = CONFIGS_DIR / "account_mappings.json"

TEMPLATES: dict[str, Path] = {
    "chase_cc": CONFIGS_DIR / "chase_cc.json",
    "cap1_cc": CONFIGS_DIR / "cap1_cc.json",
}
