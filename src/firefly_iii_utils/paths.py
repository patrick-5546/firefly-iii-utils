from pathlib import Path

from .models import TemplateInfo
from .preprocessors import preprocess_cap1_cc, preprocess_citi_cc, preprocess_wf_acct

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIGS_DIR = REPO_ROOT / "configs"
ACCOUNT_MAPPINGS_PATH = CONFIGS_DIR / "account_mappings.json"

TEMPLATES: dict[str, TemplateInfo] = {
    "chase_cc": TemplateInfo(
        path=CONFIGS_DIR / "chase_cc.json",
        filename_pattern=r"Chase(\d{4})_.*\.CSV$",
    ),
    "cap1_cc": TemplateInfo(
        path=CONFIGS_DIR / "cap1_cc.json",
        csv_column_header="Card No.",
        preprocessor=preprocess_cap1_cc,
    ),
    "bmo_harris_cc": TemplateInfo(
        path=CONFIGS_DIR / "bmo_harris_cc.json",
        filename_pattern=r"^(transactions)_\d{9}\.csv$",
    ),
    "wf_acct": TemplateInfo(
        path=CONFIGS_DIR / "wf_acct.json",
        filename_pattern=r"^(.+?) - .*\d{4}\.csv$",
        preprocessor=preprocess_wf_acct,
    ),
    "citi_cc": TemplateInfo(
        path=CONFIGS_DIR / "citi_cc.json",
        filename_pattern=r"^(Year to date).*\.CSV$",
        preprocessor=preprocess_citi_cc,
    ),
    "bilt_cc": TemplateInfo(
        path=CONFIGS_DIR / "bilt_cc.json",
        filename_pattern=r"^(transactions)-\d{4}-\d{2}-\d{2}\.csv$",
    ),
}
