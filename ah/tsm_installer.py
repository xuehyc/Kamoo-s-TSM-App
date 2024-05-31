import os
import io
import sys
import zipfile
import logging
import argparse
from typing import List, Set

import requests
from requests.adapters import HTTPAdapter, Retry

from ah import config
from ah.models.blizzard import GameVersionEnum
from ah.fs import find_warcraft_base, validate_warcraft_base


class TSMInstaller:
    ASSETS = {
        GameVersionEnum.RETAIL: {
            "https://www.tradeskillmaster.com/download/TradeSkillMaster.zip",
            "https://www.tradeskillmaster.com/download/TradeSkillMaster_AppHelper.zip",
        },
        GameVersionEnum.CLASSIC: {
            "https://www.tradeskillmaster.com/download/TradeSkillMaster-BCC.zip",
            "https://www.tradeskillmaster.com/download/TradeSkillMaster_AppHelper-BCC.zip",
        },
        GameVersionEnum.CLASSIC_ERA: {
            "https://www.tradeskillmaster.com/download/TradeSkillMaster-Classic.zip",
            "https://www.tradeskillmaster.com/download/TradeSkillMaster_AppHelper-Classic.zip",
        },
    }
    EXCLUDE = "TradeSkillMaster_AppHelper/AppData.lua"
    TIMEOUT = 10
    logger = logging.getLogger("TSMInstaller")

    def __init__(self, warcraft_base: str):
        self.warcraft_base = os.path.abspath(warcraft_base)
        self.session = requests.Session()
        retries = Retry(
            total=5,
            backoff_factor=0.1,
            status_forcelist=[500, 502, 503, 504],
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retries))
        self.session.mount("http://", HTTPAdapter(max_retries=retries))

    def _install(self, version_dir: str, assets: Set[str]) -> None:
        addon_dir = os.path.join(
            version_dir,
            "Interface",
            "AddOns",
        )
        for asset in assets:
            response = self.session.get(asset, timeout=10)
            data = io.BytesIO(response.content)
            with zipfile.ZipFile(data) as zip_ref:
                name_list = zip_ref.namelist()
                if self.EXCLUDE in name_list:
                    name_list.remove(self.EXCLUDE)
                zip_ref.extractall(addon_dir, name_list)

            asset_name = os.path.basename(asset)
            self.logger.info(f"Installed {asset_name} to {addon_dir}")

    def install(self) -> None:
        for game_version in self.ASSETS.keys():
            version_dir = os.path.join(
                self.warcraft_base, game_version.get_version_folder_name()
            )
            if not os.path.exists(version_dir):
                continue
            self._install(version_dir, self.ASSETS[game_version])


def parse_args(args: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    default_warcraft_base = find_warcraft_base()
    parser.add_argument(
        "--warcraft_base",
        type=str,
        default=default_warcraft_base,
        help="Path to Warcraft installation directory, "
        "needed if the script is unable to locate it automatically, "
        "should be something like 'C:\\path_to\\World of Warcraft'. "
        f"Auto detect: {default_warcraft_base!r}",
    )
    args = parser.parse_args()
    if not validate_warcraft_base(args.warcraft_base):
        raise ValueError(
            "Invalid Warcraft installation directory, "
            "please specify it via '--warcraft_base' option. "
            "Should be something like 'C:\\path_to\\World of Warcraft'."
        )
    return args


def main(warcraft_base: str = None) -> None:
    installer = TSMInstaller(warcraft_base)
    installer.install()


if __name__ == "__main__":
    logging.basicConfig(level=config.LOGGING_LEVEL)
    args = parse_args(sys.argv[1:])
    main(**vars(args))
