"""GLiNER 서버의 프로세스 환경 설정을 읽고 검증합니다."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


DEFAULT_MODEL_NAME = "urchade/gliner_multi-v2.1"
DEFAULT_MODEL_REVISION = (
    "443d26d654e0324125a96bebd8e796c14ff2efe6"
)
DEFAULT_BACKBONE_NAME = "microsoft/mdeberta-v3-base"
DEFAULT_BACKBONE_REVISION = (
    "a0484667b22365f84929a935b5e50a51f71f159d"
)
DEFAULT_CACHE_DIR = Path("/cache/huggingface")
DEFAULT_MAX_TEXT_CHARACTERS = 100_000
GlinerDevice = Literal["auto", "cpu", "cuda"]


def _read_non_empty_environment(
    name: str,
    default: str,
) -> str:
    """빈 값이 아닌 환경변수 문자열을 읽습니다."""

    value = os.getenv(name, default).strip()
    if not value:
        raise ValueError(f"{name}은 비어 있을 수 없습니다")
    return value


def _read_positive_integer_environment(
    name: str,
    default: int,
) -> int:
    """1 이상의 정수 환경변수를 읽습니다."""

    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError(f"{name}은 정수여야 합니다") from error
    if value < 1:
        raise ValueError(f"{name}은 1 이상이어야 합니다")
    return value


def _read_boolean_environment(
    name: str,
    default: bool,
) -> bool:
    """명시적인 true 또는 false 환경변수를 읽습니다."""

    raw_value = os.getenv(name, str(default)).strip().lower()
    if raw_value == "true":
        return True
    if raw_value == "false":
        return False
    raise ValueError(f"{name}은 true 또는 false여야 합니다")


@dataclass(frozen=True, slots=True)
class GlinerServerSettings:
    """모델 로딩과 요청 상한에 사용하는 불변 설정입니다."""

    model_name: str = DEFAULT_MODEL_NAME
    model_revision: str = DEFAULT_MODEL_REVISION
    backbone_name: str = DEFAULT_BACKBONE_NAME
    backbone_revision: str = DEFAULT_BACKBONE_REVISION
    device: GlinerDevice = "cuda"
    cache_dir: Path = DEFAULT_CACHE_DIR
    max_text_characters: int = DEFAULT_MAX_TEXT_CHARACTERS
    local_files_only: bool = False

    def __post_init__(self) -> None:
        """직접 생성된 설정도 환경변수 설정과 동일하게 검증합니다."""

        if not isinstance(self.model_name, str) or not self.model_name.strip():
            raise ValueError("model_name은 비어 있지 않은 문자열이어야 합니다")
        if (
            not isinstance(self.model_revision, str)
            or not self.model_revision.strip()
        ):
            raise ValueError(
                "model_revision은 비어 있지 않은 문자열이어야 합니다"
            )
        if (
            not isinstance(self.backbone_name, str)
            or not self.backbone_name.strip()
        ):
            raise ValueError(
                "backbone_name은 비어 있지 않은 문자열이어야 합니다"
            )
        if (
            not isinstance(self.backbone_revision, str)
            or not self.backbone_revision.strip()
        ):
            raise ValueError(
                "backbone_revision은 비어 있지 않은 문자열이어야 합니다"
            )
        if self.device not in {"auto", "cpu", "cuda"}:
            raise ValueError("device는 auto, cpu 또는 cuda여야 합니다")
        if not isinstance(self.cache_dir, Path):
            raise TypeError("cache_dir은 pathlib.Path여야 합니다")
        if (
            type(self.max_text_characters) is not int
            or self.max_text_characters < 1
        ):
            raise ValueError("max_text_characters는 1 이상의 정수여야 합니다")
        if type(self.local_files_only) is not bool:
            raise TypeError("local_files_only는 boolean이어야 합니다")

    @classmethod
    def from_environment(cls) -> GlinerServerSettings:
        """GLINER_ 접두사의 환경변수로 설정을 만듭니다."""

        device = _read_non_empty_environment(
            "GLINER_DEVICE",
            "cuda",
        ).lower()
        if device not in {"auto", "cpu", "cuda"}:
            raise ValueError(
                "GLINER_DEVICE는 auto, cpu 또는 cuda여야 합니다"
            )

        return cls(
            model_name=_read_non_empty_environment(
                "GLINER_MODEL_NAME",
                DEFAULT_MODEL_NAME,
            ),
            model_revision=_read_non_empty_environment(
                "GLINER_MODEL_REVISION",
                DEFAULT_MODEL_REVISION,
            ),
            backbone_name=_read_non_empty_environment(
                "GLINER_BACKBONE_NAME",
                DEFAULT_BACKBONE_NAME,
            ),
            backbone_revision=_read_non_empty_environment(
                "GLINER_BACKBONE_REVISION",
                DEFAULT_BACKBONE_REVISION,
            ),
            device=device,  # type: ignore[arg-type]
            cache_dir=Path(
                _read_non_empty_environment(
                    "GLINER_CACHE_DIR",
                    str(DEFAULT_CACHE_DIR),
                )
            ),
            max_text_characters=_read_positive_integer_environment(
                "GLINER_MAX_TEXT_CHARACTERS",
                DEFAULT_MAX_TEXT_CHARACTERS,
            ),
            local_files_only=_read_boolean_environment(
                "GLINER_LOCAL_FILES_ONLY",
                False,
            ),
        )


__all__ = [
    "DEFAULT_BACKBONE_NAME",
    "DEFAULT_BACKBONE_REVISION",
    "DEFAULT_CACHE_DIR",
    "DEFAULT_MAX_TEXT_CHARACTERS",
    "DEFAULT_MODEL_NAME",
    "DEFAULT_MODEL_REVISION",
    "GlinerDevice",
    "GlinerServerSettings",
]
