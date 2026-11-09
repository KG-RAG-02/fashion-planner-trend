"""파일 잠금: 같은 프로세스의 두 번째 비차단 잠금은 거부되고, 풀리면 다시 잡힌다 (POSIX·Windows 공통)."""
import pytest

from trend_agent.filelock import file_lock


def test_second_nonblocking_lock_is_refused_until_released(tmp_path):
    target = tmp_path / "calls.jsonl"
    with file_lock(target):
        with pytest.raises(BlockingIOError):
            with file_lock(target, blocking=False):
                pass
    with file_lock(target, blocking=False):  # 풀린 뒤에는 잡힌다
        pass


def test_lock_file_is_a_sidecar_and_target_is_untouched(tmp_path):
    target = tmp_path / "sub" / "calls.jsonl"
    with file_lock(target):
        assert (tmp_path / "sub" / "calls.jsonl.lock").exists()
        assert not target.exists()
