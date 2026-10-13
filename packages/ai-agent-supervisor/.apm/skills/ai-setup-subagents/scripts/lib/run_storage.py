"""Локальные данные одного запуска и их удаление при завершении задачи."""
from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path


DEFAULT_RUNS_DIR = ".local/subagents"
ARTIFACTS = {
    "record_path": "run.json",
    "final": "result.final.txt",
    "transport_path": "adapter.transport.txt",
    "transport_header_path": "adapter.transport-header.json",
    "stderr": "adapter.stderr.txt",
    "journal": "events.jsonl",
    "codex_stderr": "codex.stderr.txt",
}


def create_run_directory(out: Path, run_id: str, *, storage_verified: bool = False, git_storage: bool = False) -> Path:
    """Изолировать запуск в месте с подтверждённой защитой от VCS."""
    directory = out.resolve() / str(uuid.UUID(run_id))
    # Флаг — свидетельство предварительной проверки родителя, а не проверка VCS.
    # Без него автоматизация подтверждает только явно обнаруженный Git.
    if not storage_verified:
        if not git_storage:
            raise ValueError("VCS не проверена. Для разрешённой защиты Git передайте --git-storage, иначе после проверки места --storage-verified")
        ancestor = directory.parent
        while not ancestor.exists():
            ancestor = ancestor.parent
        try:
            result = subprocess.run(
                ["git", "-C", str(ancestor), "rev-parse", "--show-toplevel"],
                text=True, encoding="utf-8", errors="replace", capture_output=True,
                check=False,
            )
        except OSError as error:
            raise ValueError("VCS не проверена. Подтвердите защиту места и передайте --storage-verified") from error
        if result.returncode:
            raise ValueError("Git не обнаружен. Проверьте фактическую VCS или её отсутствие и передайте --storage-verified")
        root = Path(result.stdout.strip()).resolve()
        if not directory.is_relative_to(root):
            raise ValueError("место хранения находится вне проверенной области Git")
    directory.mkdir(parents=True, mode=0o700)
    try:
        if not storage_verified:
            (directory / ".gitignore").write_text("*\n", encoding="utf-8")
            result = subprocess.run(
                ["git", "-C", str(root), "check-ignore", "-q", str(directory / "run.json")],
                capture_output=True, check=False,
            )
            if result.returncode:
                raise ValueError("исключение данных запуска из Git не подтверждено")
    except (OSError, ValueError):
        (directory / ".gitignore").unlink(missing_ok=True)
        directory.rmdir()
        raise
    return directory


def artifact_paths(directory: Path) -> dict[str, Path]:
    return {key: directory / name for key, name in ARTIFACTS.items()}


def finish_run(path: Path, run_id: str, *, validate, release_retained: bool = False) -> dict:
    """Удалить только принадлежащие новому запускателю данные одного запуска."""
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("запись или каталог запуска является символьной ссылкой")
    directory = path.parent.resolve()
    if directory.name != str(uuid.UUID(run_id)) or path.name != ARTIFACTS["record_path"]:
        raise ValueError("это не отдельный каталог нового запуска: прежние журналы не удаляются")
    # Блокировка общая с записью приёмки. Чужую блокировку удалять нельзя.
    lock = directory / (path.name + ".acceptance.lock")
    locked = False
    try:
        with lock.open("x", encoding="utf-8"):
            locked = True
        record = json.loads(path.read_text(encoding="utf-8"))
        storage = record.get("storage") if isinstance(record, dict) else None
        if (not isinstance(record, dict) or record.get("run_id") != run_id
                or not isinstance(storage, dict) or type(storage.get("schema_version")) is not int
                or storage.get("schema_version") != 1):
            raise ValueError("нет подтверждённого владельца данных запуска")
        if record.get("status") not in {"completed", "failed", "timed_out", "interrupted"}:
            raise ValueError("исполнение ещё не завершено")
        if record.get("execution_class") and record.get("termination_confirmed") is not True:
            raise ValueError("завершение процессов запуска не подтверждено")
        expected = set(ARTIFACTS.values()) | {".gitignore", lock.name}
        entries = list(directory.iterdir())
        if any(entry.name not in expected or entry.is_symlink() or not entry.is_file()
               for entry in entries):
            raise ValueError("каталог содержит посторонние файлы или ссылки: очистка остановлена")
        if (directory / ".gitignore").exists() and (directory / ".gitignore").read_text(encoding="utf-8") != "*\n":
            raise ValueError("исключение каталога запуска из Git изменено")
        for key, name in ARTIFACTS.items():
            value = record.get(key)
            if value is not None and Path(value) != directory / name:
                raise ValueError("путь артефакта не принадлежит этому запуску")
        if record.get("result_path") != str(directory / ARTIFACTS["final"]):
            raise ValueError("путь результата не принадлежит этому запуску")
        validate(record)
        reason = storage.get("retain_reason")
        if not isinstance(reason, str):
            raise ValueError("причина длительного хранения имеет неправильный тип")
        if reason and not release_retained:
            return {"run_id": run_id, "storage": "retained", "reason": reason}
        # Запись и возможное исключение остаются до удаления содержательных данных.
        for entry in entries:
            if entry.name not in {lock.name, path.name, ".gitignore"}:
                entry.unlink()
        path.unlink()
        (directory / ".gitignore").unlink(missing_ok=True)
        lock.unlink()
        locked = False
        directory.rmdir()
        return {"run_id": run_id, "storage": "removed"}
    finally:
        if locked:
            lock.unlink(missing_ok=True)
