from __future__ import annotations

import unicodedata
from typing import Any

LOCATION_RESTRICTION_MESSAGE = "Siz boshqa shahardansiz."


def _normalize_location_name(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "").strip().upper())
    if not normalized:
        return ""
    return "".join(ch for ch in normalized if ch.isalnum())


def _build_district(
    myid_id: int,
    myid_name: str,
    ican_id: int,
    ican_name: str,
    *,
    aliases: tuple[str, ...] = (),
) -> dict[str, Any]:
    all_names = tuple(dict.fromkeys((myid_name, *aliases)))
    return {
        "myid_id": myid_id,
        "myid_name": myid_name,
        "myid_names": all_names,
        "ican_id": ican_id,
        "ican_name": ican_name,
        "normalized_names": {_normalize_location_name(name) for name in all_names if name},
    }


RAW_MYID_ICAN_REGIONS: dict[int, dict[str, Any]] = {
    11: {
        "myid_name": "ТОШКЕНТ ВИЛОЯТИ",
        "myid_aliases": ("ТАШКЕНТСКАЯ ОБЛАСТЬ",),
        "ican_id": 10,
        "ican_name": "Ташкентская область",
        "districts": (
            _build_district(1101, "ЯНГИЙЎЛ ТУМАНИ", 201, "Yangiyul tumani"),
            _build_district(1102, "ЗАНГИОТА ТУМАНИ", 123, "Zangi-ota tumani"),
            _build_district(1103, "ПИСКЕНТ ТУМАНИ", 126, "Piskent tumani"),
            _build_district(1104, "ПАРКЕНТ ТУМАНИ", 202, "Parkent tumani"),
            _build_district(1105, "ЎРТАЧИРЧИҚ ТУМАНИ", 127, "O`rtachirchiq tumani"),
            _build_district(1106, "ОҚҚЎРҒОН ТУМАНИ", 203, "Oqqo`rg`on tumani"),
            _build_district(1107, "ҚУЙИЧИРЧИҚ ТУМАНИ", 124, "Quyichirchiq tumani"),
            _build_district(1108, "ҚИБРАЙ ТУМАНИ", 194, "Qibray tumani"),
            _build_district(1109, "ТОШКЕНТ ТУМАНИ", 252, "Toshkent tumani"),
            _build_district(1112, "БЎСТОНЛИҚ ТУМАНИ", 250, "Bo`stonliq tumani"),
            _build_district(1114, "ЮҚОРИЧИРЧИҚ ТУМАНИ", 129, "Yuqorichirchiq tumani"),
            _build_district(1115, "ЧИНОЗ ТУМАНИ", 128, "Chinoz tumani"),
            _build_district(1116, "АНГРЕН ШАҲРИ", 251, "Angren shahri"),
            _build_district(1117, "БЕКОБОД ШАҲРИ", 196, "Bekobod shahri"),
            _build_district(1118, "ОЛМАЛИК ШАҲРИ", 197, "Olmalik shahri"),
            _build_district(1119, "ОХАНГОРОН ШАҲРИ", 198, "Oxangoron shahri", aliases=("ОҲАНГАРОН ШАҲРИ",)),
            _build_district(1120, "ЧИРЧИҚ ШАҲРИ", 199, "Chirchiq shahri"),
            _build_district(1121, "ЯНГИОБОД ШАҲРИ", 200, "Yangiobod shahri"),
            _build_district(1122, "ЯНГИЙЎЛ ШАҲРИ", 125, "Yangiyo`l shahri"),
            _build_district(739001121, "ОҲАНГАРОН ШАҲРИ", 198, "Oxangoron shahri", aliases=("ОХАНГОРОН ШАҲРИ",)),
        ),
    },
    22: {
        "myid_name": "ХОРАЗМ ВИЛОЯТИ",
        "myid_aliases": ("ХОРЕЗМСКАЯ ОБЛАСТЬ",),
        "ican_id": 13,
        "ican_name": "Хорезмская область",
        "districts": (
            _build_district(2201, "ХАЗОРАСП ТУМАНИ", 225, "Xazorasp tumani"),
            _build_district(2202, "ЯНГИАРИҚ ТУМАНИ", 226, "Yangiariq tumani"),
            _build_district(2203, "ГУРЛАН ТУМАНИ", 139, "Gurlan tumani"),
            _build_district(2204, "УРГАНЧ ТУМАНИ", 140, "Urganch tumani"),
            _build_district(2205, "ШОВОТ ТУМАНИ", 141, "Shovvot tumani"),
            _build_district(2206, "ХОНҚА ТУМАНИ", 217, "Xonqa tumani"),
            _build_district(2207, "БОҒОТ ТУМАНИ", 218, "Bog`ot tumani"),
            _build_district(2208, "ЯНГИБОЗОР ТУМАНИ", 219, "Yangibozor tumani"),
            _build_district(2209, "ҚЎШКЎПИР ТУМАНИ", 220, "Qo`shko`pir tumani"),
            _build_district(2210, "ХИВА ТУМАНИ", 221, "Xiva tumani"),
            _build_district(2211, "УРГАНЧ ШАҲРИ", 222, "Urganch shahri"),
            _build_district(2212, "ХИВА ШАҲРИ", 223, "Xiva shahri"),
            _build_district(2213, "ПИТНАК ШАҲРИ", 224, "Pitnak shahri"),
        ),
    },
    23: {
        "myid_name": "ҚОРАҚАЛПОҒИСТОН РЕСПУБЛИКАСИ",
        "myid_aliases": ("РЕСПУБЛИКА КАРАКАЛПАКСТАН",),
        "ican_id": 12,
        "ican_name": "Республика Каракалпакстан",
        "districts": (
            _build_district(2301, "НУКУС ТУМАНИ", 148, "Nukus tumani"),
            _build_district(2302, "КУНГИРОТ ТУМАНИ", 149, "Qo`ng`irot tumani"),
            _build_district(2303, "МЎЙНОҚ ТУМАНИ", 227, "Mo`ynoq tumani"),
            _build_district(2305, "ТЎРТКЎЛ ТУМАНИ", 146, "To`rtko`l tumani"),
            _build_district(2306, "ЭЛЛИКҚАЛЪА ТУМАНИ", 147, "Ellikqalla tumani"),
            _build_district(2307, "КЕГЕЙЛИ ТУМАНИ", 144, "Kegeyli tumani"),
            _build_district(2309, "БЕРУНИЙ ТУМАНИ", 143, "Beruniy tumani"),
            _build_district(2310, "КАНЛИКОЛ ТУМАНИ", 145, "Qonlikol tumani"),
            _build_district(2311, "ЧИМБОЙ ТУМАНИ", 150, "Chimboy tumani"),
            _build_district(2312, "ШУМАНАЙ ТУМАНИ", 228, "Shumanay tumani"),
            _build_district(2313, "ТАХТАКЎПИР ТУМАНИ", 229, "Taxtako`pir tumani"),
            _build_district(2314, "ХОЖЕЛИ ТУМАНИ", 230, "Xojeli tumani"),
            _build_district(2315, "БОЗАТАУ ТУМАНИ", 231, "Bazatau tumani"),
            _build_district(2316, "ҚОРАУЗОҚ ТУМАНИ", 232, "Qorauzoq tumani"),
            _build_district(2317, "НУКУС ШАҲРИ", 233, "Nukus shahri"),
            _build_district(2318, "БЕРУНИЙ ШАҲРИ", 234, "Beruniy shahri"),
            _build_district(2319, "КУНГИРОТ ШАҲРИ", 235, "Qo`ng`irot shahri"),
            _build_district(2320, "ТАКИЯТОШ ШАҲРИ", 236, "Takiyatosh shahri"),
            _build_district(2321, "ТЎРТКЎЛ ШАҲРИ", 237, "To`rtko`l shahri"),
            _build_district(2323, "ЧИМБОЙ ШАҲРИ", 239, "Chimboy shahri"),
        ),
    },
}


def _prepare_region_map(raw_regions: dict[int, dict[str, Any]]) -> dict[int, dict[str, Any]]:
    prepared: dict[int, dict[str, Any]] = {}
    for myid_region_id, region in raw_regions.items():
        region_names = tuple(dict.fromkeys((region["myid_name"], *(region.get("myid_aliases") or ()))))
        districts = tuple(region.get("districts") or ())
        prepared[myid_region_id] = {
            **region,
            "myid_id": myid_region_id,
            "myid_names": region_names,
            "normalized_names": {_normalize_location_name(name) for name in region_names if name},
            "districts": districts,
            "districts_by_id": {district["myid_id"]: district for district in districts},
        }
    return prepared


MYID_ICAN_REGIONS = _prepare_region_map(RAW_MYID_ICAN_REGIONS)


def _find_region_by_name(name: Any) -> dict[str, Any] | None:
    normalized = _normalize_location_name(name)
    if not normalized:
        return None

    for region in MYID_ICAN_REGIONS.values():
        if normalized in region["normalized_names"]:
            return region
    return None


def _find_district_by_name(region: dict[str, Any], name: Any) -> dict[str, Any] | None:
    normalized = _normalize_location_name(name)
    if not normalized:
        return None

    for district in region["districts"]:
        if normalized in district["normalized_names"]:
            return district
    return None


def resolve_ican_location(
    *,
    myid_region_id: Any,
    myid_region_name: Any = None,
    myid_district_id: Any,
    myid_district_name: Any = None,
) -> dict[str, Any]:
    try:
        normalized_region_id = int(str(myid_region_id or "").strip())
    except (TypeError, ValueError):
        normalized_region_id = 0

    region = MYID_ICAN_REGIONS.get(normalized_region_id)
    if region is None:
        region = _find_region_by_name(myid_region_name)

    if region is None:
        raise ValueError(LOCATION_RESTRICTION_MESSAGE)

    try:
        normalized_district_id = int(str(myid_district_id or "").strip())
    except (TypeError, ValueError):
        normalized_district_id = 0

    district = region["districts_by_id"].get(normalized_district_id)
    if district is not None:
        district_name = _normalize_location_name(myid_district_name)
        if district_name and district_name not in district["normalized_names"]:
            district = _find_district_by_name(region, myid_district_name)
    else:
        district = _find_district_by_name(region, myid_district_name)

    if district is None:
        raise ValueError(LOCATION_RESTRICTION_MESSAGE)

    return {
        "myid_region_id": region["myid_id"],
        "myid_region_name": region["myid_name"],
        "ican_region_id": region["ican_id"],
        "ican_region_name": region["ican_name"],
        "myid_district_id": district["myid_id"],
        "myid_district_name": district["myid_name"],
        "ican_district_id": district["ican_id"],
        "ican_district_name": district["ican_name"],
    }

