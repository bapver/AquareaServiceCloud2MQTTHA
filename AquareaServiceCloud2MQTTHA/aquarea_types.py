"""
Data types — equivalent of aquareaJsonTypes.go + internal structs from aquarea.go
"""

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Internal structs (from aquarea.go)
# ---------------------------------------------------------------------------

@dataclass
class AquareaCommand:
    device_id: str
    setting: str
    value: str


@dataclass
class AquareaFunctionDescription:
    name: str = ""
    kind: str = ""
    unit: str = ""
    min: float = -128
    max: float = 127
    step: float = 1
    values: dict[str, str] = field(default_factory=dict)
    reverse_values: dict[str, str] = field(default_factory=dict)
    label_code: str = ""    # 2010-xxxx code whose translation gives the HA entity name
    display_name: str = ""  # static fallback label if no label_code

    def __post_init__(self):
        if self.values and not self.reverse_values:
            self.reverse_values = {v: k for k, v in self.values.items()}

    @classmethod
    def from_dict(cls, d: dict) -> "AquareaFunctionDescription":
        values = d.get("values") or {}
        return cls(
            name=d.get("name", ""),
            kind=d.get("kind", ""),
            unit=d.get("unit", ""),
            min=d.get("min", -128),
            max=d.get("max", 127),
            step=d.get("step", 1),
            values=values,
            reverse_values={v: k for k, v in values.items()},
            label_code=d.get("label_code", ""),
            display_name=d.get("display_name", ""),
        )


@dataclass
class AquareaLogItem:
    name: str = ""
    unit: str = ""
    values: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# JSON types (from aquareaJsonTypes.go)
# ---------------------------------------------------------------------------

@dataclass
class AquareaEndUserJSON:
    address: str = ""
    company_id: str = ""
    connection: str = ""
    device_id: str = ""
    end_user_id: str = ""
    error_code: Any = None
    error_name: str = ""
    gw_uid: str = ""
    gwid: str = ""
    idu: str = ""
    latitude: str = ""
    longitude: str = ""
    name: str = ""
    odu: str = ""
    power: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "AquareaEndUserJSON":
        return cls(
            address=d.get("address", ""),
            company_id=d.get("companyId", ""),
            connection=d.get("connection", ""),
            device_id=d.get("deviceId", ""),
            end_user_id=d.get("enduserId", ""),
            error_code=d.get("errorCode"),
            error_name=d.get("errorName", ""),
            gw_uid=d.get("gwUid", ""),
            gwid=d.get("gwid", ""),
            idu=d.get("idu", ""),
            latitude=d.get("latitude", ""),
            longitude=d.get("longitude", ""),
            name=d.get("name", ""),
            odu=d.get("odu", ""),
            power=d.get("power", ""),
        )


@dataclass
class AquareaEndUsersListJSON:
    zoom_map: int = 0
    error_code: int = 0
    endusers: list[AquareaEndUserJSON] = field(default_factory=list)
    longitude_center_map: str = ""
    size: int = 0
    latitude_center_map: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "AquareaEndUsersListJSON":
        return cls(
            zoom_map=d.get("zoomMap", 0),
            error_code=d.get("errorCode", 0),
            endusers=[AquareaEndUserJSON.from_dict(u) for u in d.get("endusers", [])],
            longitude_center_map=d.get("longitudeCenterMap", ""),
            size=d.get("size", 0),
            latitude_center_map=d.get("latitudeCenterMap", ""),
        )


@dataclass
class StatusDataItem:
    value: str = ""
    text_value: str = ""
    type: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "StatusDataItem":
        return cls(
            value=d.get("value", ""),
            text_value=d.get("textValue", ""),
            type=d.get("type", ""),
        )


@dataclass
class AquareaStatusResponseJSON:
    error_code: int = 0
    status_data_info: dict[str, StatusDataItem] = field(default_factory=dict)
    status_background_data_info: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "AquareaStatusResponseJSON":
        return cls(
            error_code=d.get("errorCode", 0),
            status_data_info={
                k: StatusDataItem.from_dict(v)
                for k, v in d.get("statusDataInfo", {}).items()
            },
            status_background_data_info=d.get("statusBackgroundDataInfo", {}),
        )


@dataclass
class AquareaLogDataJSON:
    error_history: list[dict] = field(default_factory=list)
    log_data: str = ""
    error_code: int = 0
    recording_status: int = 0
    history_no: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "AquareaLogDataJSON":
        return cls(
            error_history=d.get("errorHistory", []),
            log_data=d.get("logData", ""),
            error_code=d.get("errorCode", 0),
            recording_status=d.get("recordingStatus", 0),
            history_no=d.get("historyNo", ""),
        )


@dataclass
class AquareaLoginJSON:
    agreement_status: dict = field(default_factory=dict)
    error_code: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> "AquareaLoginJSON":
        return cls(
            agreement_status=d.get("agreementStatus", {}),
            error_code=d.get("errorCode", 0),
        )


@dataclass
class SettingDataItem:
    type: str = ""
    selected_value: str = ""
    placeholder: str = ""
    params: dict[str, str] = field(default_factory=dict)
    text_value: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "SettingDataItem":
        return cls(
            type=d.get("type", ""),
            selected_value=d.get("selectedValue", ""),
            placeholder=d.get("placeholder", ""),
            params=d.get("params") or {},
            text_value=d.get("textValue", ""),
        )


@dataclass
class AquareaFunctionSettingGetJSON:
    setting_data_info: dict[str, SettingDataItem] = field(default_factory=dict)
    settings_background_data: dict[str, dict] = field(default_factory=dict)
    raw_setting_data_info: dict[str, dict] = field(default_factory=dict)
    error_code: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> "AquareaFunctionSettingGetJSON":
        raw = d.get("settingDataInfo", {})
        return cls(
            setting_data_info={
                k: SettingDataItem.from_dict(v)
                for k, v in raw.items()
            },
            settings_background_data=d.get("settingBackgroundData", {}),
            raw_setting_data_info=raw,
            error_code=d.get("errorCode", 0),
        )