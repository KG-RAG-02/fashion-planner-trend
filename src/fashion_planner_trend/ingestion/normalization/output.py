"""산출물 쓰기 안전장치.

- 호출자가 읽기 전용으로 지정한 폴더(`protected`)거나 그 안이면 `--out`을 거부한다.
- 쓰려는 파일이 심볼릭 링크면 거부한다(링크를 통해 원본을 덮어쓸 수 있다).
- 임시 파일에 쓴 뒤 rename한다(중간에 실패해도 반쯤 쓴 파일이 남지 않는다).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Iterable


class UnsafeOutput(ValueError):
    pass


def resolve_out(out: Path | str, protected: Iterable[Path] = ()) -> Path:
    raw = Path(out)
    if raw.is_symlink():
        raise UnsafeOutput(f"--out이 심볼릭 링크다: {raw}")
    path = raw.resolve()
    for p in protected:
        p = Path(p).resolve()
        if path == p or p in path.parents:
            raise UnsafeOutput(f"--out이 읽기 전용 폴더다: {path}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _check_target(path: Path) -> None:
    if path.is_symlink():
        raise UnsafeOutput(f"대상 파일이 심볼릭 링크다(링크를 통해 원본을 덮어쓸 수 있다): {path}")


def write_text_atomic(path: Path, text: str) -> None:
    _check_target(path)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_lines_atomic(path: Path, lines: Iterable[str]) -> int:
    _check_target(path)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    n = 0
    with tmp.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")
            n += 1
    os.replace(tmp, path)
    return n


def write_json_atomic(path: Path, obj: Any) -> None:
    write_text_atomic(path, json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False) + "\n")


def sha256_file(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_state(repo: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        try:
            return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            return ""
    return {"commit": run("rev-parse", "HEAD"), "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(run("status", "--porcelain", "--", "src", "configs", "scripts"))}


def update_build_info(out_dir: Path, key: str, info: dict[str, Any]) -> None:
    """BUILD_INFO.json의 key만 바꾼다(02·03 스크립트가 같은 파일을 나눠 쓴다)."""
    path = out_dir / "BUILD_INFO.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data[key] = info
    write_json_atomic(path, data)
