"""생성·후처리 기록(manifest.json)과 파일 이름 규칙.

- 기록은 결과 파일이 놓인 폴더의 manifest.json 에 덧붙인다.
- 여러 Claude 세션이 같은 폴더에 동시에 쓸 수 있으므로 잠금 파일로 직렬화하고, 임시 파일 → 교체로 원자적으로 쓴다.
- 파일 이름: {용도}_{짧은설명}_{YYYYMMDD}_{번호}.{확장자}  예) sfx_kick_heavy_20261028_01.wav
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

MANIFEST_NAME = "manifest.json"


def slug(text: str, fallback: str, max_len: int = 32) -> str:
    """파일 이름용 영소문자·숫자·밑줄 문자열. 한글 등은 제거되므로 설명은 영어로 받는 게 좋다."""
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return (s[:max_len].strip("_")) or fallback


def next_audio_path(folder: Path, purpose: str, description: str, ext: str = "wav",
                    now: datetime | None = None) -> Path:
    """같은 용도·설명·날짜 접두사 중 가장 큰 번호 + 1 로 새 경로를 만든다."""
    folder.mkdir(parents=True, exist_ok=True)
    date = (now or datetime.now()).strftime("%Y%m%d")
    prefix = f"{slug(purpose, 'sfx', 12)}_{slug(description, 'sound')}_{date}_"
    pattern = re.compile(re.escape(prefix) + r"(\d+)\.")
    used = [int(m.group(1)) for p in folder.iterdir() if (m := pattern.match(p.name))]
    return folder / f"{prefix}{(max(used) if used else 0) + 1:02d}.{ext}"


@contextmanager
def _locked(folder: Path, timeout_s: float = 10.0) -> Iterator[None]:
    lock = folder / (MANIFEST_NAME + ".lock")
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            # 비정상 종료로 남은 오래된 잠금은 치운다.
            if time.monotonic() > deadline or time.time() - lock.stat().st_mtime > 60:
                lock.unlink(missing_ok=True)
                continue
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(fd)
        lock.unlink(missing_ok=True)


def load(folder: Path) -> list[dict[str, Any]]:
    path = folder / MANIFEST_NAME
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def append(folder: Path, entry: dict[str, Any], service: str) -> dict[str, Any]:
    """기록 한 건을 덧붙이고, id·service·created_at 이 채워진 항목을 돌려준다."""
    folder.mkdir(parents=True, exist_ok=True)
    record = {
        "id": f"m_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}",
        "service": service,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        **entry,
    }
    with _locked(folder):
        records = load(folder)
        records.append(record)
        tmp = folder / (MANIFEST_NAME + ".tmp")
        tmp.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, folder / MANIFEST_NAME)
    return record


def find_by_file(folder: Path, file_path: str) -> dict[str, Any] | None:
    """해당 파일을 결과로 가진 가장 최근 기록."""
    for rec in reversed(load(folder)):
        if file_path in rec.get("files", []):
            return rec
    return None


def search(folder: Path, query: str | None = None, tool: str | None = None,
           limit: int = 20) -> list[dict[str, Any]]:
    """최근 순. query 는 프롬프트·설명·메모·파일 경로에서 대소문자 무시 부분 일치."""
    rows = []
    for rec in reversed(load(folder)):
        if tool and rec.get("tool") != tool:
            continue
        if query:
            hay = json.dumps({k: rec.get(k) for k in ("params", "note", "files", "description")},
                             ensure_ascii=False).lower()
            if query.lower() not in hay:
                continue
        rows.append(rec)
        if len(rows) >= limit:
            break
    return rows
