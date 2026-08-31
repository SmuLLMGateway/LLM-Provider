"""애플리케이션 전역에서 공유하는 기반 기능을 제공합니다."""

from app.core.json_codec import (
    StrictJsonDecodeError,
    StrictJsonDecodeErrorCode,
    StrictJsonEncodeError,
    dump_canonical_json_utf8,
    dump_json_utf8,
    load_strict_json,
)
from app.core.json_value import (
    InvalidJsonValueError,
    InvalidJsonValueErrorCode,
    JsonScalar,
    JsonValue,
    normalize_json_value,
)
from app.core.registry_file_coordinator import (
    DEFAULT_REGISTRY_FILE_COORDINATOR,
    RegistryFileCoordinator,
)

__all__ = [
    "DEFAULT_REGISTRY_FILE_COORDINATOR",
    "InvalidJsonValueError",
    "InvalidJsonValueErrorCode",
    "JsonScalar",
    "JsonValue",
    "RegistryFileCoordinator",
    "StrictJsonDecodeError",
    "StrictJsonDecodeErrorCode",
    "StrictJsonEncodeError",
    "dump_canonical_json_utf8",
    "dump_json_utf8",
    "load_strict_json",
    "normalize_json_value",
]
