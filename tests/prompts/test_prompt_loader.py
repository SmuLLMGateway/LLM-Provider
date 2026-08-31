"""단일 고정 Prompt 파일의 제한된 UTF-8 로드를 검증합니다."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.prompts.prompt_errors import (
    PromptTemplateDecodeError,
    PromptTemplateNotFoundError,
    PromptTemplateReadError,
    PromptTemplateTooLargeError,
)
from app.prompts.prompt_limits import PromptLimits
from app.prompts.prompt_loader import (
    DEFAULT_PROMPT_PATH,
    PromptLoader,
)


def test_default_prompt_path_points_to_config_file() -> None:
    """기본 경로가 프로젝트의 config/prompts.j2로 고정되어 있습니다."""

    project_root = Path(__file__).resolve().parents[2]

    assert DEFAULT_PROMPT_PATH == project_root / "config" / "prompts.j2"


def test_loads_utf8_prompt_and_normalizes_newlines(
    tmp_path: Path,
) -> None:
    """고정 파일의 UTF-8 본문과 줄바꿈을 정규화합니다."""

    prompt_path = tmp_path / "prompts.j2"
    prompt_path.write_bytes(
        "원문:\r\n{{ text }}\r기존: {{ existing_detections }}\r\n".encode(
            "utf-8"
        )
    )

    loaded = PromptLoader(prompt_path).load()

    assert loaded == (
        "원문:\n{{ text }}\n기존: {{ existing_detections }}\n"
    )


def test_load_reflects_file_changes_without_cache(
    tmp_path: Path,
) -> None:
    """Loader는 캐시하지 않고 Builder가 최초 결과의 수명을 관리합니다."""

    prompt_path = tmp_path / "prompts.j2"
    loader = PromptLoader(prompt_path)
    prompt_path.write_text("version-1", encoding="utf-8")

    first = loader.load()
    prompt_path.write_text("version-2", encoding="utf-8")
    second = loader.load()

    assert first == "version-1"
    assert second == "version-2"


def test_rejects_missing_prompt_file(tmp_path: Path) -> None:
    """고정 파일이 없으면 명시적인 NotFound 오류를 반환합니다."""

    prompt_path = tmp_path / "prompts.j2"

    with pytest.raises(PromptTemplateNotFoundError) as error_info:
        PromptLoader(prompt_path).load()

    assert error_info.value.template_path == str(prompt_path.resolve())


def test_rejects_directory_instead_of_prompt_file(
    tmp_path: Path,
) -> None:
    """고정 파일 위치가 디렉터리이면 일반 파일로 취급하지 않습니다."""

    prompt_path = tmp_path / "prompts.j2"
    prompt_path.mkdir()

    with pytest.raises(PromptTemplateNotFoundError):
        PromptLoader(prompt_path).load()


def test_rejects_non_utf8_prompt(tmp_path: Path) -> None:
    """UTF-8로 해석할 수 없는 고정 파일을 거부합니다."""

    prompt_path = tmp_path / "prompts.j2"
    prompt_path.write_bytes(b"\xff\xfe")

    with pytest.raises(PromptTemplateDecodeError) as error_info:
        PromptLoader(prompt_path).load()

    assert error_info.value.template_path == str(prompt_path.resolve())


def test_template_size_limit_uses_utf8_bytes(
    tmp_path: Path,
) -> None:
    """문자 수가 아닌 UTF-8 byte 수로 고정 파일 크기를 제한합니다."""

    prompt_path = tmp_path / "prompts.j2"
    loader = PromptLoader(
        prompt_path,
        limits=PromptLimits(max_template_bytes=6),
    )
    prompt_path.write_text("가나", encoding="utf-8")
    assert loader.load() == "가나"

    prompt_path.write_text("가나다", encoding="utf-8")

    with pytest.raises(PromptTemplateTooLargeError) as error_info:
        loader.load()

    assert error_info.value.actual_bytes == 9
    assert error_info.value.max_bytes == 6


def test_loader_reads_at_most_limit_plus_one_byte(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """파일 크기 확인 뒤에도 bounded read만 수행합니다."""

    prompt_path = tmp_path / "prompts.j2"
    limits = PromptLimits(max_template_bytes=7)
    requested_sizes: list[int] = []

    class TrackingFile:
        """PromptLoader의 open/read 계약을 기록하는 binary file 대역입니다."""

        def __enter__(self) -> TrackingFile:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def fileno(self) -> int:
            return 123

        def read(self, size: int) -> bytes:
            requested_sizes.append(size)
            return b"prompt"

    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr(
        Path,
        "open",
        lambda self, mode: TrackingFile(),
    )
    monkeypatch.setattr(
        "app.prompts.prompt_loader.os.fstat",
        lambda file_descriptor: SimpleNamespace(st_size=6),
    )

    assert PromptLoader(prompt_path, limits=limits).load() == "prompt"
    assert requested_sizes == [limits.max_template_bytes + 1]


def test_wraps_file_read_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """파일 시스템 오류를 원본 경로가 있는 공통 Prompt 오류로 변환합니다."""

    prompt_path = tmp_path / "prompts.j2"
    monkeypatch.setattr(Path, "is_file", lambda self: True)

    def fail_open(self: Path, mode: str):
        del self, mode
        raise OSError("read failed")

    monkeypatch.setattr(Path, "open", fail_open)

    with pytest.raises(PromptTemplateReadError) as error_info:
        PromptLoader(prompt_path).load()

    assert error_info.value.template_path == str(prompt_path.resolve())
