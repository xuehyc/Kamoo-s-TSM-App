import re
import os
import time
from logging import getLogger
from typing import Optional

from ah.api import API
from ah.storage import BinaryFile, TextFile
from ah.models import (
    AuctionsResponse,
    CommoditiesResponse,
    MapItemStringMarketValueRecord,
    MapItemStringMarketValueRecords,
)
from ah.defs import SECONDS_IN
from ah.tsm_exporter import TSMExporter


class TaskManager:

    # TODO: add init and inject api object and maybe more
    # crid=connected realm id
    FN_REGION_COMMODITIES = "{region}-commodities.{suffix}"
    FN_CRID_AUCTIONS = "{region}-{crid}-auctions.{suffix}"
    MIN_RECORDS_EXPIRES_IN = 60 * SECONDS_IN.DAY

    REALM_EXPORTS = [
        {
            "type": "AUCTIONDB_REALM_DATA",
            "sources": ["auctions"],
            "fields": [
                "itemString",
                "minBuyout",
                "numAuctions",
                "marketValueRecent",
            ],
        },
        {
            "type": "AUCTIONDB_REALM_HISTORICAL",
            "sources": ["auctions"],
            "fields": [
                "itemString",
                "historical",
            ],
        },
        {
            "type": "AUCTIONDB_REALM_SCAN_STAT",
            "sources": ["auctions"],
            "fields": [
                "itemString",
                "marketValue",
            ],
        },
    ]
    COMMODITIES_EXPORT = {
        "type": "AUCTIONDB_REGION_COMMODITY",
        "sources": ["commodities"],
        "fields": [
            "itemString",
            "minBuyout",
            "numAuctions",
            "marketValueRecent",
        ],
    }
    REGION_EXPORTS = [
        {
            "type": "AUCTIONDB_REGION_STAT",
            "sources": ["auctions", "commodities"],
            "fields": [
                "itemString",
                "regionMarketValue",
            ],
        },
        {
            "type": "AUCTIONDB_REGION_HISTORICAL",
            "sources": ["auctions", "commodities"],
            "fields": [
                "itemString",
                "regionHistorical",
            ],
        },
    ]

    def __init__(
        self,
        api: API,
        data_path: str,
        data_use_compression: bool = True,
        records_expires_in: int = MIN_RECORDS_EXPIRES_IN,
    ) -> None:
        self._logger = getLogger(self.__class__.__name__)
        if records_expires_in < self.MIN_RECORDS_EXPIRES_IN:
            # we need at least 60 days for historical
            raise ValueError("Increment expires too short!")

        self.api = api
        self.data_path = data_path
        self.data_use_compression = data_use_compression
        self.records_expires_in = records_expires_in

    def _pull_connected_realms_ids(self, region: str):
        connected_realms = self.api.get_connected_realms_index(region)
        for cr in connected_realms["connected_realms"]:
            ret = re.search(r"connected-realm/(\d+)", cr["href"])
            crid = ret.group(1)
            yield int(crid)

    def _pull_connected_realm(self, region: str, crid: int):
        """
        >>> ret = {
            # crid
            "id": 123,
            "timezone": "Asia/Taipei",
            "realms": [
                {
                    "id": 123,
                    "name": "Realm Name",
                    "slug": "realm-slug"
                },
                ...
            ]
        }
        """
        connected_realm = self.api.get_connected_realm(region, crid)
        ret = {"id": crid, "realms": []}
        for realm in connected_realm["realms"]:
            if "timezone" in ret and ret["timezone"] != realm["timezone"]:
                raise ValueError(
                    "Timezone differes between realms under same connected realm!"
                )

            else:
                ret["timezone"] = realm["timezone"]

            ret["realms"].append(
                {
                    "id": realm["id"],
                    "name": realm["name"],
                    "slug": realm["slug"],
                    "locale": realm["locale"],
                }
            )

        return ret

    def _pull_connected_realms(self, region: str):
        """

        >>> {
                # connected realms
                $crid: $crid_data,
                ...
            }
        """
        crids = self._pull_connected_realms_ids(region)
        ret = {}
        for crid in crids:
            connected_realm = self._pull_connected_realm(region, crid)
            ret[crid] = connected_realm

        return ret

    def _get_timezone(self, region, connected_realm_id):
        """NOTE: CRs under same region may have different timezones!"""
        connected_realm = self._api.get_connected_realm(region, connected_realm_id)
        return connected_realm["realms"][0]["timezone"]

    def pull_increment(
        self, region: str, crid: int = None
    ) -> Optional[MapItemStringMarketValueRecord]:
        if crid:
            try:
                resp = AuctionsResponse.from_api(self.api, region, crid)
            except Exception as e:
                self._logger.error(
                    f"Failed to request auctions for {region}-{crid}: {e}"
                )
                return
        else:
            try:
                resp = CommoditiesResponse.from_api(self.api, region)
            except Exception as e:
                self._logger.error(f"Failed to request commodities for {region}: {e}")
                return

        increment = MapItemStringMarketValueRecord.from_response(resp)
        return increment

    def update_db(
        self, file: BinaryFile, increment: MapItemStringMarketValueRecord, ts_now: int
    ) -> Optional["MapItemStringMarketValueRecords"]:
        """
        Update db file.

        if only region given, update commodities file
        if both region and crid given, update auctions file

        return updated records

        """
        if file.exists():
            records_map = MapItemStringMarketValueRecords.from_file(file)
        else:
            records_map = MapItemStringMarketValueRecords()
        n_added_records, n_added_entries = records_map.update_increment(increment)
        n_removed_records = records_map.remove_expired(ts_now - self.records_expires_in)
        records_map.to_file(file)
        self._logger.info(
            f"DB update: {file.file_path}, {n_added_records=} "
            f"{n_added_entries=} {n_removed_records=}"
        )
        return records_map

    def get_db_file(self, region: str, crid: int = None) -> BinaryFile:
        """
        if only region given, get commodities file
        if both region and crid given, get auctions file

        """
        fn_suffix = "gz" if self.data_use_compression else "bin"
        if crid:
            fn = self.FN_CRID_AUCTIONS.format(
                region=region, crid=crid, suffix=fn_suffix
            )
        else:
            fn = self.FN_REGION_COMMODITIES.format(region=region, suffix=fn_suffix)
        file_path = os.path.join(self.data_path, fn)
        file = BinaryFile(file_path, use_compression=self.data_use_compression)
        return file

    def update_dbs_under_region(
        self,
        region: str,
    ) -> None:
        crids = self._pull_connected_realms_ids(region)
        ts_update_begin = int(time.time())

        for crid in crids:
            increment = self.pull_increment(region, crid)
            if increment:
                file = self.get_db_file(region, crid=crid)
                self.update_db(file, increment, ts_update_begin)

        increment = self.pull_increment(region)
        if increment:
            file = self.get_db_file(region)
            self.update_db(file, increment, ts_update_begin)
