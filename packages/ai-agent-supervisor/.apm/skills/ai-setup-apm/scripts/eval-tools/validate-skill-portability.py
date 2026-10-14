#!/usr/bin/env python3
"""Проверить декларации переносимости и зависимости скриптов навыков."""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

# Русские сообщения не должны падать на консоли с однобайтовой кодировкой.
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        _reconfigure(encoding="utf-8", errors="replace")


SCRIPT_SUFFIXES = {".py", ".sh", ".bash"}
CONTRACT_RUNNER = Path(__file__).with_name("run-skill-script-contract-tests.py")
HIDDEN_INSTALL_PATTERNS = (
    re.compile(r"\bpip(?:3)?\s+install\b"),
    re.compile(r"\buv\s+run\b"),
    re.compile(r"\bnpx\b"),
)
SKILL_COMMAND_PATTERNS = (
    ("POSIX-команда sh -c", re.compile(r"\bsh\s+-c\b")),
    ("жёстко заданный запуск python3", re.compile(r"\bpython3\b")),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Проверить переносимость поставляемых навыков.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[Path(".apm/skills")],
        help="Каталоги навыков или отдельные навыки.",
    )
    return parser.parse_args()


def skill_directories(paths: list[Path]) -> list[Path]:
    result: set[Path] = set()
    for path in paths:
        if (path / "SKILL.md").is_file():
            result.add(path)
        elif path.is_dir():
            result.update(file.parent for file in path.rglob("SKILL.md"))
    return sorted(result)


def frontmatter_value(text: str, key: str) -> str | None:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end < 0:
        return None
    lines = text[4:end].splitlines()
    for index, line in enumerate(lines):
        if not line.startswith(f"{key}:"):
            continue
        value = line.split(":", 1)[1].strip()
        if value not in {">", "|"}:
            return value
        folded: list[str] = []
        for following in lines[index + 1 :]:
            if following and not following[0].isspace():
                break
            folded.append(following.strip())
        return " ".join(folded).strip()
    return None


def script_files(skill: Path) -> list[Path]:
    scripts = skill / "scripts"
    if not scripts.is_dir():
        return []
    result: list[Path] = []
    for path in scripts.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix in SCRIPT_SUFFIXES:
            result.append(path)
            continue
        try:
            first_line = path.open(encoding="utf-8").readline()
        except (OSError, UnicodeError):
            continue
        if first_line.startswith("#!"):
            result.append(path)
    return sorted(result)


def called_name(func: ast.AST) -> str:
    """Получить имя вызываемого без разбора выражения."""
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def prints_non_ascii(tree: ast.AST) -> bool:
    """Найти печать текста за пределами ASCII."""
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and called_name(node.func) == "print"):
            continue
        for value in ast.walk(node):
            if (
                isinstance(value, ast.Constant)
                and isinstance(value.value, str)
                and not value.value.isascii()
            ):
                return True
    return False


def switches_output_to_utf8(tree: ast.AST) -> bool:
    """Найти переключение стандартных потоков в UTF-8."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if called_name(node.func).lstrip("_") != "reconfigure":
            continue
        for keyword in node.keywords:
            if (
                keyword.arg == "encoding"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value == "utf-8"
            ):
                return True
    return False


def child_encoding_errors(tree: ast.AST) -> list[tuple[int, str]]:
    """Различить двоичный, текстовый и статически неизвестный режимы."""
    errors: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if called_name(node.func) not in {"run", "Popen", "check_output"}:
            continue
        keywords = {keyword.arg: keyword.value for keyword in node.keywords}
        values: dict[str, bool | None] = {}
        for name in ("text", "universal_newlines", "encoding", "errors"):
            expression = keywords.get(name)
            if expression is None:
                values[name] = False
            else:
                try:
                    values[name] = bool(ast.literal_eval(expression))
                except (ValueError, TypeError):
                    values[name] = None
        # subprocess включает текстовый режим и через encoding или errors.
        if not any(value is True for value in values.values()):
            if any(value is None for value in values.values()):
                errors.append((node.lineno, "не удалось определить режим дочернего процесса статически"))
            continue
        if values["encoding"] is None:
            errors.append((node.lineno, "не удалось проверить encoding дочернего процесса статически"))
        elif not values["encoding"]:
            errors.append((
                node.lineno,
                "вывод дочернего процесса читается в текстовом режиме без encoding; "
                "кодовая страница системы не разберёт сообщения вне ASCII",
            ))
    return errors


# Модули, чей open не работает с текстом через параметр encoding.
OPEN_MODULES_WITHOUT_ENCODING = {
    # tokenize.open сам определяет кодировку по объявлению файла.
    "tokenize",
    # os.open возвращает файловый дескриптор, кодировки у него нет.
    "os",
}

# Символы, из которых состоит режим открытия файла.
OPEN_MODE_CHARACTERS = set("rwxabt+U")


def open_mode(node: ast.Call) -> ast.expr | None:
    """Вернуть выражение режима открытия файла, если оно задано."""
    for keyword in node.keywords:
        if keyword.arg == "mode":
            return keyword.value
    if isinstance(node.func, ast.Attribute):
        # У Path.open режим стоит первым, у io.open — вторым.
        candidates = node.args[:2]
    else:
        candidates = node.args[1:2]
    for argument in candidates:
        if (
            isinstance(argument, ast.Constant)
            and isinstance(argument.value, str)
            and argument.value
            and set(argument.value) <= OPEN_MODE_CHARACTERS
        ):
            return argument
    for argument in candidates:
        if not isinstance(argument, ast.Constant):
            return argument
    return None


def opens_text_file(node: ast.Call) -> bool:
    """Проверить, что вызов open заведомо открывает текстовый файл."""
    mode = open_mode(node)
    if mode is None:
        return True
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return "b" not in mode.value
    # Режим задан выражением, поэтому текстовый режим не подтверждён.
    return False


def text_file_without_encoding(tree: ast.AST) -> int | None:
    """Найти работу с текстовым файлом без заданной кодировки."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = called_name(node.func)
        if name not in {"read_text", "write_text", "open"}:
            continue
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in OPEN_MODULES_WITHOUT_ENCODING
        ):
            continue
        if any(keyword.arg == "encoding" for keyword in node.keywords):
            continue
        if name == "open" and not opens_text_file(node):
            continue
        return node.lineno
    return None


def python_tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def contained_file(root: Path, relative: object) -> Path | None:
    """Найти поставляемый файл, исключив абсолютные пути и выход через ссылки."""
    if not isinstance(relative, str) or not relative.strip():
        return None
    path = PurePosixPath(relative)
    if (
        path.is_absolute() or PureWindowsPath(relative).drive
        or "\\" in relative or ".." in path.parts
    ):
        return None
    candidate = root / path
    try:
        if candidate.resolve().is_relative_to(root.resolve()) and candidate.is_file():
            return candidate
    except (OSError, RuntimeError, ValueError):
        # Неправильный путь или цикл ссылок является ошибкой декларации.
        return None
    return None


def local_module(skill: Path, script: Path, module: str, level: int = 0) -> bool:
    """Разрешить импорт по файлам навыка, без импорта кода и поиска в окружении."""
    scripts = (skill / "scripts").resolve()
    if level:
        base = script.parent
        for _ in range(level - 1):
            base = base.parent
        bases = [base]
    else:
        # Каталог команды и корень пакетов, загружаемых этой командой.
        bases = [script.parent, skill / "scripts"]
    for base in bases:
        candidate = base.joinpath(*module.split(".")) if module else base
        if not candidate.resolve().is_relative_to(scripts):
            continue
        if module and contained_file(skill, candidate.with_suffix(".py").relative_to(skill).as_posix()):
            return True
        if candidate.is_dir():
            if any(path.resolve().is_relative_to(scripts) for path in candidate.rglob("*.py") if path.is_file()):
                return True
    return False


def python_imports(tree: ast.AST, skill: Path, script: Path) -> set[str]:
    """Найти внешние импорты, сохранив квалифицированные и относительные пути."""
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(
                alias.name.split(".", 1)[0] for alias in node.names
                if not local_module(skill, script, alias.name)
            )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if not local_module(skill, script, module, node.level):
                imports.add("." * node.level + module if node.level else module.split(".", 1)[0])
    return imports


def dependency_exceptions(skill: Path) -> tuple[set[tuple[str, str]], list[str]]:
    """Проверить полноту исключений. Подлинность решения проверяет владелец."""
    relative = "references/dependency-exceptions.json"
    manifest = skill / relative
    allowed: set[tuple[str, str]] = set()
    errors: list[str] = []
    if not manifest.exists():
        return allowed, errors
    if contained_file(skill, relative) is None:
        return allowed, [f"{manifest}: файл исключений должен находиться внутри навыка"]
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        return allowed, [f"{manifest}: не удалось прочитать JSON: {error}"]
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        return allowed, [f"{manifest}: требуется schema_version: 1"]
    dependencies = data.get("dependencies")
    if not isinstance(dependencies, list):
        return allowed, [f"{manifest}: dependencies должен быть массивом"]
    for index, record in enumerate(dependencies):
        label = f"{manifest}: dependencies[{index}]"
        if not isinstance(record, dict):
            errors.append(f"{label}: требуется объект исключения")
            continue
        problems: list[str] = []

        def require_text(obj: dict, key: str, prefix: str = "") -> str:
            value = obj.get(key)
            if not isinstance(value, str) or not value.strip():
                problems.append(f"{prefix}{key}: требуется непустая строка")
                return ""
            return value

        for key in ("package", "version", "justification", "source", "license", "absence_behavior", "basic_route"):
            require_text(record, key)
        if type(record.get("network")) is not bool:
            problems.append("network: требуется true или false")
        modules = record.get("imports")
        if not isinstance(modules, list) or not modules or not all(
            isinstance(name, str) and name.isidentifier() for name in modules
        ):
            problems.append("imports: требуется непустой массив имён корневых модулей")
        scripts = record.get("scripts")
        if not isinstance(scripts, list) or not scripts or not all(
            isinstance(path, str) and path.startswith("scripts/")
            and contained_file(skill, path) is not None for path in scripts
        ):
            problems.append("scripts: требуются существующие файлы внутри scripts/")
        approval = record.get("approval")
        if not isinstance(approval, dict):
            problems.append("approval: требуются owner и decision")
        else:
            if approval.get("status") != "approved":
                problems.append("approval.status: требуется approved после решения владельца")
            require_text(approval, "owner", "approval.")
            decision = require_text(approval, "decision", "approval.")
            evidence = contained_file(skill, decision)
            if evidence is None:
                problems.append("approval.decision: требуется файл решения внутри навыка")
            else:
                try:
                    if not evidence.read_text(encoding="utf-8").strip():
                        problems.append("approval.decision: файл решения пуст")
                except (OSError, UnicodeError):
                    problems.append("approval.decision: решение должно читаться как UTF-8")
        installation = record.get("installation")
        if not isinstance(installation, dict):
            problems.append("installation: требуются command и integrity")
        else:
            require_text(installation, "command", "installation.")
            integrity = require_text(installation, "integrity", "installation.")
            if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", integrity):
                problems.append("installation.integrity: требуется sha256:<64 шестнадцатеричных символа>")
        if problems:
            errors.extend(f"{label}: {problem}" for problem in problems)
            continue
        for path in scripts:
            for module in modules:
                pair = (contained_file(skill, path).relative_to(skill).as_posix(), module)
                if pair in allowed:
                    errors.append(f"{label}: повторное исключение для {path} и {module}")
                allowed.add(pair)
    return allowed, errors


def validate_skill(skill: Path) -> list[str]:
    errors: list[str] = []
    skill_file = skill / "SKILL.md"
    text = skill_file.read_text(encoding="utf-8")
    scripts = script_files(skill)
    if not scripts:
        return errors
    allowed_imports, exception_errors = dependency_exceptions(skill)
    errors.extend(exception_errors)

    compatibility = frontmatter_value(text, "compatibility")
    if not compatibility:
        errors.append(f"{skill_file}: нет поля compatibility для навыка со скриптами")
        compatibility = ""
    if "## Переносимость" not in text or "P0" not in text:
        errors.append(f"{skill_file}: не описан базовый маршрут P0 в разделе «Переносимость»")
    if not re.search(r"недоступ|отсутств", text, re.IGNORECASE):
        errors.append(f"{skill_file}: не описано поведение при недоступной автоматизации")

    uses_python = False
    uses_posix = False
    for script in scripts:
        content = script.read_text(encoding="utf-8")
        first_line = content.splitlines()[0] if content.splitlines() else ""
        is_python = script.suffix == ".py" or "python" in first_line.lower()
        uses_python = uses_python or is_python
        uses_posix = uses_posix or bool(re.search(r"/(?:ba|z|k)?sh\b", first_line))
        for pattern in HIDDEN_INSTALL_PATTERNS:
            if pattern.search(content):
                errors.append(f"{script}: скрытая установка или загрузка зависимости")
        if is_python:
            try:
                tree = python_tree(script)
            except SyntaxError as error:
                errors.append(f"{script}:{error.lineno}: не удалось разобрать Python")
                continue
            line = text_file_without_encoding(tree)
            if line is not None:
                errors.append(
                    f"{script}:{line}: текстовый файл читается или пишется без "
                    "encoding; кодовая страница системы исказит содержимое "
                    "вне ASCII",
                )
            for line, message in child_encoding_errors(tree):
                errors.append(f"{script}:{line}: {message}")
            if prints_non_ascii(tree) and not switches_output_to_utf8(tree):
                errors.append(
                    f"{script}: печатает текст вне ASCII, но не переключает "
                    "стандартные потоки в UTF-8; на консоли с однобайтовой "
                    "кодировкой команда завершится ошибкой кодирования",
                )
            imports = python_imports(tree, skill, script)
            external = sorted(
                module for module in imports - set(sys.stdlib_module_names) - {"__future__"}
                if (script.relative_to(skill).as_posix(), module) not in allowed_imports
            )
            if external:
                errors.append(
                    f"{script}: сторонние импорты запрещены без полного согласованного "
                    f"исключения: {', '.join(external)}"
                )

    if uses_python and "Python" not in compatibility:
        errors.append(f"{skill_file}: compatibility не объявляет Python для P1")
    if uses_posix and "POSIX" not in compatibility:
        errors.append(f"{skill_file}: compatibility не объявляет POSIX для P2")
    for label, pattern in SKILL_COMMAND_PATTERNS:
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            errors.append(f"{skill_file}:{line}: {label} в основной инструкции")
    return errors


def run_script_contracts(paths: list[Path]) -> int:
    if not CONTRACT_RUNNER.is_file():
        print(
            f"Не найден запускатель контрактов скриптов: {CONTRACT_RUNNER}",
            file=sys.stderr,
        )
        return 2
    result = subprocess.run(
        [sys.executable, str(CONTRACT_RUNNER), *(str(path) for path in paths)],
        check=False,
    )
    return result.returncode


def main() -> int:
    args = parse_args()
    missing = [str(path) for path in args.paths if not path.exists()]
    if missing:
        print(f"Пути не найдены: {', '.join(missing)}", file=sys.stderr)
        return 2
    errors = [
        error
        for skill in skill_directories(args.paths)
        for error in validate_skill(skill)
    ]
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    contracts_result = run_script_contracts(args.paths)
    if contracts_result != 0:
        return contracts_result
    print("Декларации переносимости и зависимости скриптов проверены.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
