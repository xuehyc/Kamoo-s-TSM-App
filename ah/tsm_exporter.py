from typing import List, Set
import argparse
import logging
import sys
import os

import numpy as np

from ah.models import (
    MapItemStringMarketValueRecords,
    RegionEnum,
    Namespace,
    NameSpaceCategoriesEnum,
    GameVersionEnum,
    DBTypeEnum,
    FactionEnum,
    Meta,
    MarketValueRecords,
    RealmCategoryEnum,
)
from ah.storage import TextFile
from ah.db import DBHelper, GithubFileForker
from ah.api import GHAPI
from ah.cache import Cache
from ah.utils import find_warcraft_base, validate_warcraft_base
from ah import config


class TSMExporter:
    REALM_AUCTIONS_EXPORTS = [
        {
            "type": "AUCTIONDB_NON_COMMODITY_DATA",
            "sources": ["auctions"],
            "desc": "auction latest market value",
            "fields": [
                "itemString",
                "minBuyout",
                "numAuctions",
                "marketValueRecent",
            ],
            "per_faction": True,
        },
        {
            "type": "AUCTIONDB_NON_COMMODITY_HISTORICAL",
            "sources": ["auctions"],
            "desc": "auction monthly market value",
            "fields": [
                "itemString",
                "historical",
            ],
            "per_faction": True,
        },
        {
            "type": "AUCTIONDB_NON_COMMODITY_SCAN_STAT",
            "sources": ["auctions"],
            "desc": "auction two week market value",
            "fields": [
                "itemString",
                "marketValue",
            ],
            "per_faction": True,
        },
    ]
    REGION_COMMODITIES_EXPORTS = [
        {
            "type": "AUCTIONDB_COMMODITY_DATA",
            "sources": ["commodities"],
            "desc": "commodity latest market value",
            "fields": [
                "itemString",
                "minBuyout",
                "numAuctions",
                "marketValueRecent",
            ],
        },
        {
            "type": "AUCTIONDB_COMMODITY_HISTORICAL",
            "sources": ["commodities"],
            "desc": "commodity monthly market value",
            "fields": [
                "itemString",
                "historical",
            ],
        },
        {
            "type": "AUCTIONDB_COMMODITY_SCAN_STAT",
            "sources": ["commodities"],
            "desc": "commodity two week market value",
            "fields": [
                "itemString",
                "marketValue",
            ],
        },
    ]
    REGION_AUCTIONS_COMMODITIES_EXPORTS = [
        {
            "type": "AUCTIONDB_REGION_STAT",
            "sources": ["auctions", "commodities"],
            "desc": "region auctions two week data",
            "fields": [
                "itemString",
                "regionMarketValue",
            ],
        },
        {
            "type": "AUCTIONDB_REGION_HISTORICAL",
            "sources": ["auctions", "commodities"],
            "desc": "region auctions monthly data",
            "fields": [
                "itemString",
                "regionHistorical",
            ],
        },
    ]
    TEMPLATE_ROW = (
        'select(2, ...).LoadData("{data_type}","{region_or_realm}",[[return '
        "{{downloadTime={ts},fields={{{fields}}},data={{{data}}}}}]])"
    )
    TEMPLATE_APPDATA = (
        'select(2, ...).LoadData("APP_INFO","Global",[[return '
        "{{version={version},lastSync={last_sync},"
        'message={{id=0,msg=""}},news={{}}}}]])'
    )
    NUMERIC_SET = set("0123456789")
    TSM_VERSION = 41200
    TSM_HC_LABEL = "HC"
    TSM_SEASONAL_LABEL = "SoD"
    _logger = logging.getLogger("TSMExporter")

    def __init__(
        self,
        db_helper: DBHelper,
        export_file: TextFile,
        forker: GithubFileForker = None,
    ) -> None:
        self.db_helper = db_helper
        self.export_file = export_file
        self.forker = forker

    @classmethod
    def get_tsm_appdata_path(
        cls, warcraft_base: str, game_version: GameVersionEnum
    ) -> str:
        return os.path.join(
            warcraft_base,
            game_version.get_version_folder_name(),
            "Interface",
            "AddOns",
            "TradeSkillMaster_AppHelper",
            "AppData.lua",
        )

    @classmethod
    def baseN(cls, num, b, numerals="0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
        return ((num == 0) and numerals[0]) or (
            cls.baseN(num // b, b, numerals).lstrip(numerals[0]) + numerals[num % b]
        )

    @classmethod
    def export_append_data(
        cls,
        file: TextFile,
        map_records: MapItemStringMarketValueRecords,
        fields: List[str],
        type_: str,
        region_or_realm: str,
        ts_update_begin: int,
        ts_update_end: int,
        should_reset_tsc: bool = False,
    ) -> None:
        cls._logger.info(f"Exporting {type_} for {region_or_realm}...")
        if should_reset_tsc:
            ts_compressed = 0
        else:
            ts_compressed = MarketValueRecords.get_compress_end_ts(ts_update_begin)
        items_data = []
        for item_string, records in map_records.items():
            # tsm can handle:
            # 1. numeral itemstring being string
            # 2. 10-based numbers
            item_data = []
            # skip item if all numbers are 0 or None
            is_skip_item = True
            for field in fields:
                if field == "minBuyout":
                    value = records.get_recent_min_buyout(ts_update_begin)
                    if value:
                        is_skip_item = False
                elif field == "numAuctions":
                    value = records.get_recent_num_auctions(ts_update_begin)
                    if value:
                        is_skip_item = False
                elif field == "marketValueRecent":
                    value = records.get_recent_market_value(ts_update_begin)
                    if value:
                        is_skip_item = False
                elif field in ["historical", "regionHistorical"]:
                    value = records.get_historical_market_value(
                        ts_update_end, ts_compressed=ts_compressed
                    )
                    if value:
                        is_skip_item = False
                elif field in ["marketValue", "regionMarketValue"]:
                    value = records.get_weighted_market_value(
                        ts_update_end, ts_compressed=ts_compressed
                    )
                    if value:
                        is_skip_item = False
                elif field == "itemString":
                    value = item_string.to_str()
                    if not set(value) < cls.NUMERIC_SET:
                        value = '"' + value + '"'
                else:
                    raise ValueError(f"unsupported field {field}.")

                if isinstance(value, (int, np.int32, np.int64)):
                    value = cls.baseN(value, 32)
                elif isinstance(value, float):
                    value = str(value)
                elif isinstance(value, str):
                    pass
                else:
                    raise ValueError(f"unsupported type {type(value)}")

                item_data.append(value)

            if is_skip_item:
                # XXX: this occurs very often for 15-day and last scan data types,
                # because records outside the time range are still there, therefore
                # the item entry (item string) too.
                # cls._logger.debug(
                #     f"During {type_}, {item_string} skipped, "
                #     f"due to all fields are empty."
                # )
                continue

            item_text = "{" + ",".join(item_data) + "}"
            items_data.append(item_text)

        fields_str = ",".join('"' + field + '"' for field in fields)
        text_out = cls.TEMPLATE_ROW.format(
            data_type=type_,
            region_or_realm=region_or_realm,
            ts=ts_update_begin,
            fields=fields_str,
            data=",".join(items_data),
        )
        with file.open("a", encoding="utf-8") as f:
            f.write(text_out + "\n")

    def export_region(
        self,
        namespace: Namespace,
        export_realms: Set[str],
    ):
        meta_file = self.db_helper.get_file(namespace, DBTypeEnum.META)
        if self.forker:
            meta_file.remove()
        meta = Meta.from_file(meta_file, forker=self.forker)
        if not meta:
            raise ValueError(f"meta file {meta_file} not found or Empty.")
        ts_update_start, ts_update_end = meta.get_update_ts()

        all_realms = set(meta.get_connected_realm_names())
        if not export_realms <= all_realms:
            raise ValueError(f"unavailable realms : {export_realms - all_realms}. ")

        # determines if we need to export data of each category - if user selected
        # realms under a category, we need to export that category
        cate_should_export = dict()
        for cate in RealmCategoryEnum:
            cate_should_export[cate] = False

        # tracks auctions + commodities (if applicable) for all realms under this category
        cate_data = dict()
        for cate in RealmCategoryEnum:
            cate_data[cate] = MapItemStringMarketValueRecords()

        if namespace.game_version == GameVersionEnum.RETAIL:
            commodity_file = self.db_helper.get_file(namespace, DBTypeEnum.COMMODITIES)
            if self.forker:
                commodity_file.remove()
            commodity_data = MapItemStringMarketValueRecords.from_file(
                commodity_file, forker=self.forker
            )
        else:
            commodity_file = None
            commodity_data = None

        if commodity_data:
            # only retail has commodities, it only has `RealmCategoryEnum.DEFAULT`
            cate_data[RealmCategoryEnum.DEFAULT].extend(commodity_data)

            for commodity_export in self.REGION_COMMODITIES_EXPORTS:
                self.export_append_data(
                    self.export_file,
                    commodity_data,
                    commodity_export["fields"],
                    commodity_export["type"],
                    namespace.region.upper(),
                    ts_update_start,
                    ts_update_end,
                )

        if namespace.game_version == GameVersionEnum.RETAIL:
            factions = [None]
        else:
            factions = [FactionEnum.ALLIANCE, FactionEnum.HORDE]

        for crid, connected_realms, category in meta.iter_connected_realms():
            # find all realm names we want to export under this connected realm,
            # they share the same auction data
            sub_export_realms = export_realms & connected_realms

            for faction in factions:
                db_file = self.db_helper.get_file(
                    namespace,
                    DBTypeEnum.AUCTIONS,
                    crid=crid,
                    faction=faction,
                )
                if self.forker:
                    db_file.remove()
                auction_data = MapItemStringMarketValueRecords.from_file(
                    db_file, forker=self.forker
                )
                if not auction_data:
                    self._logger.warning(f"no data in {db_file}.")
                    continue

                cate_data[category].extend(auction_data)

                if not sub_export_realms:
                    continue
                else:
                    cate_should_export[category] = True

                for realm in sub_export_realms:
                    if faction is None:
                        tsm_realm = realm
                    else:
                        tsm_realm = f"{realm}-{faction.get_full_name()}"

                    for realm_auctions_export in self.REALM_AUCTIONS_EXPORTS:
                        self.export_append_data(
                            self.export_file,
                            auction_data,
                            realm_auctions_export["fields"],
                            realm_auctions_export["type"],
                            tsm_realm,
                            ts_update_start,
                            ts_update_end,
                        )

        for cate, data in cate_data.items():
            if not data:
                continue

            if not cate_should_export[cate]:
                continue

            region = namespace.region.upper()
            if cate == RealmCategoryEnum.HARDCORE:
                part = self.TSM_HC_LABEL
            elif cate == RealmCategoryEnum.SEASONAL:
                part = self.TSM_SEASONAL_LABEL
            else:
                part = namespace.game_version.get_tsm_game_version()

            if part:
                tsm_region = f"{part}-{region}"
            else:
                # retail = None
                tsm_region = region

            # need to sort because it's records are from multiple realms
            data.sort()
            for region_a_c_export in self.REGION_AUCTIONS_COMMODITIES_EXPORTS:
                self.export_append_data(
                    self.export_file,
                    data,
                    region_a_c_export["fields"],
                    region_a_c_export["type"],
                    tsm_region,
                    ts_update_start,
                    ts_update_end,
                    should_reset_tsc=True,
                )

        self.export_append_app_info(self.export_file, self.TSM_VERSION, ts_update_end)

    @classmethod
    def export_append_app_info(cls, file: TextFile, version: int, ts_last_sync: int):
        with file.open("a", encoding="utf-8") as f:
            text_out = cls.TEMPLATE_APPDATA.format(
                version=version,
                last_sync=ts_last_sync,
            )
            f.write(text_out + "\n")


def main(
    db_path: str = None,
    repo: str = None,
    gh_proxy: str = None,
    game_version: GameVersionEnum = None,
    warcraft_base: str = None,
    export_region: RegionEnum = None,
    export_realms: Set[str] = None,
    # below are for testability
    cache: Cache = None,
    gh_api: GHAPI = None,
):
    if repo:
        cache = cache or Cache(config.DEFAULT_CACHE_PATH)
        cache.remove_expired()
        gh_api = gh_api or GHAPI(cache, gh_proxy=gh_proxy)
        forker = GithubFileForker(repo, gh_api)
    else:
        forker = None

    db_helper = DBHelper(db_path)
    export_path = TSMExporter.get_tsm_appdata_path(warcraft_base, game_version)
    namespace = Namespace(
        category=NameSpaceCategoriesEnum.DYNAMIC,
        game_version=game_version,
        region=export_region,
    )
    export_file = TextFile(export_path)
    exporter = TSMExporter(db_helper, export_file, forker=forker)
    exporter.export_file.remove()
    exporter.export_region(namespace, export_realms)


def parse_args(raw_args):
    parser = argparse.ArgumentParser()
    default_db_path = config.DEFAULT_DB_PATH
    default_game_version = GameVersionEnum.RETAIL.name.lower()
    default_warcraft_base = find_warcraft_base()

    parser.add_argument(
        "--db_path",
        type=str,
        default=default_db_path,
        help=f"path to the database, default: {default_db_path!r}",
    )
    parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help="Address of Github repo that's hosting the db files. If given, "
        "download and use repo's db instead of local ones. "
        "Note: local db will be overwritten.",
    )
    parser.add_argument(
        "--gh_proxy",
        type=str,
        default=None,
        help="URL of Github proxy server, for people having trouble accessing Github "
        "while using --repo option. "
        "Read more at https://github.com/crazypeace/gh-proxy, "
        "this program need a modified version that hosts API requests: "
        "https://github.com/hunshcn/gh-proxy/issues/44",
    )
    parser.add_argument(
        "--game_version",
        choices={e.name.lower() for e in GameVersionEnum},
        default=default_game_version,
        help=f"Game version to export, default: {default_game_version!r}",
    )
    parser.add_argument(
        "--warcraft_base",
        type=str,
        default=default_warcraft_base,
        help="Path to Warcraft installation directory, "
        "needed if the script is unable to locate it automatically, "
        "should be something like 'C:\\path_to\\World of Warcraft'. "
        f"Auto detect: {default_warcraft_base!r}",
    )
    parser.add_argument(
        "export_region",
        choices={e.value for e in RegionEnum},
        help="Region to export",
    )
    parser.add_argument(
        "export_realms",
        type=str,
        nargs="+",
        help="Realms to export, separated by space.",
    )
    args = parser.parse_args(raw_args)

    if args.repo and not GithubFileForker.validate_repo(args.repo):
        raise ValueError(
            f"Invalid Github repo given by '--repo' option, "
            f"it should be a valid Github repo URL, not {args.repo!r}."
        )
    if args.repo and args.gh_proxy and not GHAPI.validate_gh_proxy(args.gh_proxy):
        raise ValueError(
            f"Invalid Github proxy server given by '--gh_proxy' option, "
            f"it should be a valid URL, not {args.gh_proxy!r}."
        )
    if not validate_warcraft_base(args.warcraft_base):
        raise ValueError(
            "Invalid Warcraft installation directory, "
            "please specify it via '--warcraft_base' option. "
            "Should be something like 'C:\\path_to\\World of Warcraft'."
        )
    args.game_version = GameVersionEnum[args.game_version.upper()]
    args.export_region = RegionEnum(args.export_region)
    args.export_realms = set(args.export_realms)

    return args


if __name__ == "__main__":
    logging.basicConfig(level=config.LOGGING_LEVEL)
    args = parse_args(sys.argv[1:])
    main(**vars(args))
