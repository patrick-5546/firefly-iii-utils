from pathlib import Path

from .models import TemplateInfo
from .preprocessors import preprocess_cap1_cc, preprocess_citi_cc, preprocess_wf_acct

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIGS_DIR = REPO_ROOT / "configs"
ACCOUNT_MAPPINGS_PATH = CONFIGS_DIR / "account_mappings.json"
TEMPLATE_DETECTION_PATH = CONFIGS_DIR / "template_detection.json"

TEMPLATES: dict[str, TemplateInfo] = {
    "chase_cc": TemplateInfo(path=CONFIGS_DIR / "chase_cc.json"),
    "cap1_cc": TemplateInfo(
        path=CONFIGS_DIR / "cap1_cc.json",
        preprocessor=preprocess_cap1_cc,
    ),
    "bmo_harris_cc": TemplateInfo(path=CONFIGS_DIR / "bmo_harris_cc.json"),
    "wf_acct": TemplateInfo(
        path=CONFIGS_DIR / "wf_acct.json",
        preprocessor=preprocess_wf_acct,
    ),
    "citi_cc": TemplateInfo(
        path=CONFIGS_DIR / "citi_cc.json",
        preprocessor=preprocess_citi_cc,
    ),
}
