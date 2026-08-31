"""LPL의 공통 HTTP NER 계약을 구현하는 GLiNER 서버입니다."""

from .main import app, create_app


__all__ = ["app", "create_app"]
