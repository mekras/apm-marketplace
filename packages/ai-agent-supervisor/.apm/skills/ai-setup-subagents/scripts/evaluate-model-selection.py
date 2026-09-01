#!/usr/bin/env python3
"""Оценить последовательный выбор модели по готовой матрице запусков."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import statistics
import sys
import tempfile
from pathlib import Path
from statistics import NormalDist
from typing import Any


KINDS = {"normal", "boundary", "escalation", "forbidden"}
SPLITS = {"tuning", "holdout"}
ELIGIBILITY_GATES = (
    "available",
    "actual_model_verified",
    "required_capabilities",
    "within_budget",
)


class InputError(ValueError):
    """Ошибка проверяемого входного набора."""


def number(value: object, label: str, minimum: float = 0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f"{label}: ожидалось число")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise InputError(f"{label}: число должно быть не меньше {minimum}")
    return result


def integer(value: object, label: str, minimum: int = 0) -> int:
    result = number(value, label, minimum)
    if not result.is_integer():
        raise InputError(f"{label}: ожидалось целое число")
    return int(result)


def boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise InputError(f"{label}: ожидалось логическое значение")
    return value


def records(value: object, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise InputError(f"{label}: ожидался массив записей")
    return value


def unique_id(item: dict[str, Any], label: str) -> str:
    value = item.get("id")
    if not isinstance(value, str) or not value.strip():
        raise InputError(f"{label}.id: нужна непустая строка")
    return value


def load_input(path: Path) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
    except OSError as error:
        raise InputError(f"не удалось прочитать вход: {error}") from error
    except json.JSONDecodeError as error:
        raise InputError(f"входной JSON не разобран: {error}") from error
    if not isinstance(data, dict):
        raise InputError("корень входа должен быть объектом")
    return data, hashlib.sha256(raw).hexdigest()


def validate_settings(data: dict[str, Any]) -> dict[str, float | int]:
    settings = data.get("settings")
    if not isinstance(settings, dict):
        raise InputError("settings: нужна запись настроек")
    result: dict[str, float | int] = {
        "confidence_level": number(settings.get("confidence_level"), "settings.confidence_level"),
        "quality_margin_pp": number(
            settings.get("quality_margin_pp"), "settings.quality_margin_pp"
        ),
        "refutation_gap_pp": number(
            settings.get("refutation_gap_pp"), "settings.refutation_gap_pp"
        ),
        "confirmation_savings_percent": number(
            settings.get("confirmation_savings_percent"),
            "settings.confirmation_savings_percent",
        ),
        "refutation_savings_percent": number(
            settings.get("refutation_savings_percent"),
            "settings.refutation_savings_percent",
        ),
        "max_review_regression_percent": number(
            settings.get("max_review_regression_percent"),
            "settings.max_review_regression_percent",
        ),
        "initial_cases": integer(settings.get("initial_cases"), "settings.initial_cases", 1),
        "batch_cases": integer(settings.get("batch_cases"), "settings.batch_cases", 1),
        "minimum_repeats": integer(settings.get("minimum_repeats"), "settings.minimum_repeats", 1),
        "required_holdout_runs_per_candidate": integer(
            settings.get("required_holdout_runs_per_candidate"),
            "settings.required_holdout_runs_per_candidate",
            1,
        ),
    }
    if not 0.5 < result["confidence_level"] < 1:
        raise InputError("settings.confidence_level: нужно значение между 0.5 и 1")
    if result["quality_margin_pp"] > 100 or result["refutation_gap_pp"] > 100:
        raise InputError("границы качества не могут превышать 100 процентных пунктов")
    if result["refutation_gap_pp"] < result["quality_margin_pp"]:
        raise InputError("refutation_gap_pp не может быть меньше quality_margin_pp")
    if result["refutation_savings_percent"] > result["confirmation_savings_percent"]:
        raise InputError(
            "refutation_savings_percent не может превышать confirmation_savings_percent"
        )
    return result


def validate_candidates(data: dict[str, Any]) -> tuple[list[str], dict[str, list[str]]]:
    candidates = records(data.get("candidates"), "candidates")
    seen: set[str] = set()
    eligible: list[str] = []
    excluded: dict[str, list[str]] = {}
    for index, candidate in enumerate(candidates):
        candidate_id = unique_id(candidate, f"candidates[{index}]")
        if candidate_id in seen:
            raise InputError(f"повторный кандидат: {candidate_id}")
        seen.add(candidate_id)
        gates = candidate.get("eligibility")
        if not isinstance(gates, dict):
            raise InputError(f"кандидат {candidate_id}: нужна запись eligibility")
        reasons = []
        for gate in ELIGIBILITY_GATES:
            if not boolean(gates.get(gate), f"кандидат {candidate_id}.{gate}"):
                reasons.append(gate)
        if boolean(
            gates.get("repeated_deterministic_failure"),
            f"кандидат {candidate_id}.repeated_deterministic_failure",
        ):
            reasons.append("repeated_deterministic_failure")
        if reasons:
            excluded[candidate_id] = reasons
        else:
            eligible.append(candidate_id)
    if not seen:
        raise InputError("candidates: нужен хотя бы один кандидат")
    return eligible, excluded


def validate_cases(data: dict[str, Any]) -> tuple[list[str], list[str], dict[str, dict[str, Any]]]:
    cases = records(data.get("cases"), "cases")
    by_id: dict[str, dict[str, Any]] = {}
    indexed: list[tuple[int, int, str]] = []
    kinds: dict[str, int] = {kind: 0 for kind in KINDS}
    for index, case in enumerate(cases):
        case_id = unique_id(case, f"cases[{index}]")
        if case_id in by_id:
            raise InputError(f"повторный случай: {case_id}")
        split = case.get("split")
        kind = case.get("kind")
        if split not in SPLITS:
            raise InputError(f"случай {case_id}: split должен быть tuning или holdout")
        if kind not in KINDS:
            raise InputError(f"случай {case_id}: неизвестный kind")
        order = integer(case.get("order", index), f"случай {case_id}.order")
        by_id[case_id] = case
        indexed.append((0 if split == "tuning" else 1, order, case_id))
        if split == "tuning":
            kinds[kind] += 1
    tuning = [case_id for split, _, case_id in sorted(indexed) if split == 0]
    holdout = [case_id for split, _, case_id in sorted(indexed) if split == 1]
    if len(tuning) < 6:
        raise InputError("настроечная выборка должна содержать не менее шести случаев")
    if kinds["normal"] < 2 or kinds["boundary"] < 2:
        raise InputError("нужны минимум два обычных и два пограничных настроечных случая")
    if kinds["escalation"] < 1 or kinds["forbidden"] < 1:
        raise InputError("нужны случаи остановки и привлекательного запрещённого действия")
    if not holdout:
        raise InputError("нужна отдельная отложенная выборка")
    return tuning, holdout, by_id


def validate_runs(
    data: dict[str, Any],
    candidate_ids: set[str],
    case_ids: set[str],
    eligible: list[str],
    minimum_repeats: int,
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    runs = records(data.get("runs"), "runs")
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    keys: set[tuple[str, str, int]] = set()
    for index, run in enumerate(runs):
        candidate = run.get("candidate_id")
        case = run.get("case_id")
        if candidate not in candidate_ids:
            raise InputError(f"runs[{index}]: неизвестный candidate_id")
        if case not in case_ids:
            raise InputError(f"runs[{index}]: неизвестный case_id")
        repeat = integer(run.get("repeat"), f"runs[{index}].repeat", 1)
        key = (candidate, case, repeat)
        if key in keys:
            raise InputError(f"повторный запуск: {candidate}, {case}, {repeat}")
        keys.add(key)
        boolean(run.get("accepted_without_revision"), f"runs[{index}].accepted_without_revision")
        boolean(run.get("critical_defect"), f"runs[{index}].critical_defect")
        number(run.get("model_cost_units"), f"runs[{index}].model_cost_units")
        number(run.get("review_seconds"), f"runs[{index}].review_seconds")
        if "total_cost_units" in run:
            number(run["total_cost_units"], f"runs[{index}].total_cost_units")
        grouped.setdefault((candidate, case), []).append(run)
    for candidate in eligible:
        for case in case_ids:
            count = len(grouped.get((candidate, case), []))
            if count < minimum_repeats:
                raise InputError(
                    f"для {candidate} и {case} нужно минимум "
                    f"{minimum_repeats} запусков, есть {count}"
                )
    return grouped


def wilson(successes: int, count: int, confidence: float) -> tuple[float, float]:
    if count == 0:
        return 0.0, 1.0
    z = NormalDist().inv_cdf(0.5 + confidence / 2)
    rate = successes / count
    denominator = 1 + z * z / count
    centre = (rate + z * z / (2 * count)) / denominator
    spread = z * math.sqrt(rate * (1 - rate) / count + z * z / (4 * count * count)) / denominator
    return max(0.0, centre - spread), min(1.0, centre + spread)


def selected_runs(
    grouped: dict[tuple[str, str], list[dict[str, Any]]],
    candidates: list[str],
    cases: list[str],
) -> list[dict[str, Any]]:
    return [run for candidate in candidates for case in cases for run in grouped[(candidate, case)]]


def metrics(runs: list[dict[str, Any]], confidence: float) -> dict[str, Any]:
    accepted = sum(run["accepted_without_revision"] for run in runs)
    low, high = wilson(accepted, len(runs), confidence)
    total_values = [run.get("total_cost_units") for run in runs]
    return {
        "runs": len(runs),
        "accepted": accepted,
        "acceptance_rate": accepted / len(runs) if runs else 0,
        "acceptance_interval": [low, high],
        "critical_defects": sum(run["critical_defect"] for run in runs),
        "model_cost_units": sum(run["model_cost_units"] for run in runs),
        "total_cost_units": (
            sum(total_values) if runs and all(value is not None for value in total_values) else None
        ),
        "median_review_seconds": statistics.median(
            [run["review_seconds"] for run in runs]
        ) if runs else None,
    }


def cheaper_key(candidate: str, item: dict[str, Any]) -> tuple[float, float, str]:
    cost = item["total_cost_units"]
    if cost is None:
        cost = item["model_cost_units"]
    return cost / max(item["runs"], 1), -item["acceptance_rate"], candidate


def sequential_selection(
    eligible: list[str],
    tuning: list[str],
    grouped: dict[tuple[str, str], list[dict[str, Any]]],
    settings: dict[str, float | int],
) -> tuple[str | None, set[tuple[str, str, int]], list[dict[str, Any]]]:
    active = list(eligible)
    revealed: set[tuple[str, str, int]] = set()
    rounds: list[dict[str, Any]] = []
    cursor = 0
    size = int(settings["initial_cases"])
    confidence = float(settings["confidence_level"])
    margin = float(settings["quality_margin_pp"]) / 100
    while cursor < len(tuning) and len(active) > 1:
        batch = tuning[cursor : cursor + size]
        cursor += len(batch)
        size = int(settings["batch_cases"])
        before = list(active)
        for candidate in before:
            for case in batch:
                for run in grouped[(candidate, case)]:
                    revealed.add((candidate, case, run["repeat"]))
        used_cases = tuning[:cursor]
        summaries = {
            candidate: metrics(selected_runs(grouped, [candidate], used_cases), confidence)
            for candidate in before
        }
        eliminated: dict[str, str] = {}
        for candidate in list(active):
            if summaries[candidate]["critical_defects"]:
                active.remove(candidate)
                eliminated[candidate] = "critical_defect"
        if active:
            leader = max(
                active,
                key=lambda item: (
                    summaries[item]["acceptance_rate"],
                    -cheaper_key(item, summaries[item])[0],
                ),
            )
            leader_low = summaries[leader]["acceptance_interval"][0]
            for candidate in list(active):
                if candidate == leader:
                    continue
                if summaries[candidate]["acceptance_interval"][1] < leader_low - margin:
                    active.remove(candidate)
                    eliminated[candidate] = "quality_interval"
        rounds.append(
            {
                "cases_opened": batch,
                "active_before": before,
                "eliminated": eliminated,
                "active_after": list(active),
                "metrics": summaries,
            }
        )
    if not active:
        return None, revealed, rounds
    used_cases = tuning[:cursor] if cursor else tuning[: int(settings["initial_cases"])]
    summaries = {
        candidate: metrics(selected_runs(grouped, [candidate], used_cases), confidence)
        for candidate in active
    }
    best_rate = max(item["acceptance_rate"] for item in summaries.values())
    leader = min(
        (candidate for candidate in active if summaries[candidate]["acceptance_rate"] == best_rate),
        key=lambda item: cheaper_key(item, summaries[item]),
    )
    leader_low = summaries[leader]["acceptance_interval"][0]
    comparable = [
        candidate
        for candidate in active
        if summaries[candidate]["acceptance_interval"][1]
        >= leader_low - float(settings["quality_margin_pp"]) / 100
    ]
    return min(comparable, key=lambda item: cheaper_key(item, summaries[item])), revealed, rounds


def percent_saving(part: float | None, whole: float | None) -> float | None:
    if part is None or whole is None or whole <= 0:
        return None
    return (whole - part) / whole * 100


def review_regression(chosen: dict[str, Any], baseline: dict[str, Any]) -> float | None:
    left = chosen["median_review_seconds"]
    right = baseline["median_review_seconds"]
    if left is None or right is None:
        return None
    if right == 0:
        return 0.0 if left == 0 else None
    return (left - right) / right * 100


def evaluate(data: dict[str, Any], digest: str) -> dict[str, Any]:
    if data.get("version") != 1:
        raise InputError("version: поддерживается только версия 1")
    execution_class = data.get("execution_class")
    if not isinstance(execution_class, str) or not execution_class:
        raise InputError("execution_class: нужна непустая строка")
    contract_sha256 = data.get("contract_sha256")
    if not isinstance(contract_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", contract_sha256
    ):
        raise InputError("contract_sha256: нужен SHA-256 контракта в нижнем регистре")
    settings = validate_settings(data)
    eligible, excluded = validate_candidates(data)
    tuning, holdout, cases = validate_cases(data)
    candidate_ids = {
        unique_id(item, f"candidates[{index}]")
        for index, item in enumerate(records(data.get("candidates"), "candidates"))
    }
    grouped = validate_runs(
        data,
        candidate_ids,
        set(cases),
        eligible,
        int(settings["minimum_repeats"]),
    )
    chosen, revealed, rounds = sequential_selection(eligible, tuning, grouped, settings)
    full_tuning_runs = selected_runs(grouped, eligible, tuning) if eligible else []
    sequential_runs = [
        run
        for candidate, case, repeat in revealed
        for run in grouped[(candidate, case)]
        if run["repeat"] == repeat
    ]
    confidence = float(settings["confidence_level"])
    full_cost = metrics(full_tuning_runs, confidence)
    sequential_cost = metrics(sequential_runs, confidence)
    cost_comparison = {
        "full_matrix_model_cost_units": full_cost["model_cost_units"],
        "sequential_model_cost_units": sequential_cost["model_cost_units"],
        "model_cost_savings_percent": percent_saving(
            sequential_cost["model_cost_units"], full_cost["model_cost_units"]
        ),
        "full_matrix_total_cost_units": full_cost["total_cost_units"],
        "sequential_total_cost_units": sequential_cost["total_cost_units"],
        "total_cost_savings_percent": percent_saving(
            sequential_cost["total_cost_units"], full_cost["total_cost_units"]
        ),
    }
    report: dict[str, Any] = {
        "version": 1,
        "execution_class": execution_class,
        "contract_sha256": contract_sha256,
        "input_sha256": digest,
        "eligible_candidates": eligible,
        "excluded_candidates": excluded,
        "selected_candidate": chosen,
        "rounds": rounds,
        "cost_comparison": cost_comparison,
        "hypothesis_status": "insufficient_data",
        "policy_action": "keep_current_or_reference_route",
        "reasons": [],
    }
    if len(eligible) < 2:
        report["reasons"].append("для сравнения нужны минимум два прошедших отсев кандидата")
        return report
    if chosen is None:
        report["reasons"].append("все кандидаты исключены последовательной проверкой")
        return report

    holdout_metrics = {
        candidate: metrics(selected_runs(grouped, [candidate], holdout), confidence)
        for candidate in eligible
    }
    viable = [
        candidate
        for candidate in eligible
        if not holdout_metrics[candidate]["critical_defects"]
    ]
    if viable:
        best_rate = max(holdout_metrics[candidate]["acceptance_rate"] for candidate in viable)
        baseline = min(
            (
                candidate
                for candidate in viable
                if holdout_metrics[candidate]["acceptance_rate"] == best_rate
            ),
            key=lambda item: cheaper_key(item, holdout_metrics[item]),
        )
    else:
        baseline = None
    chosen_metrics = holdout_metrics[chosen]
    baseline_metrics = holdout_metrics[baseline] if baseline else None
    report["holdout"] = {
        "metrics": holdout_metrics,
        "full_matrix_baseline": baseline,
        "required_runs_per_candidate": int(settings["required_holdout_runs_per_candidate"]),
    }
    if baseline_metrics is None:
        report["reasons"].append("у всех кандидатов есть критические дефекты на отложенной выборке")
        return report
    if chosen == baseline:
        gap_point = gap_low = gap_high = 0.0
    else:
        gap_point = (baseline_metrics["acceptance_rate"] - chosen_metrics["acceptance_rate"]) * 100
        gap_low = max(
            0.0,
            (
                baseline_metrics["acceptance_interval"][0]
                - chosen_metrics["acceptance_interval"][1]
            )
            * 100,
        )
        gap_high = min(
            100.0,
            (
                baseline_metrics["acceptance_interval"][1]
                - chosen_metrics["acceptance_interval"][0]
            )
            * 100,
        )
    regression = review_regression(chosen_metrics, baseline_metrics)
    comparison = {
        "quality_gap_pp": gap_point,
        "quality_gap_interval_pp": [gap_low, gap_high],
        "review_time_regression_percent": regression,
        "chosen_critical_defects": chosen_metrics["critical_defects"],
        "baseline_critical_defects": baseline_metrics["critical_defects"],
    }
    report["holdout"]["comparison"] = comparison
    sufficient = all(
        holdout_metrics[candidate]["runs"]
        >= int(settings["required_holdout_runs_per_candidate"])
        for candidate in eligible
    )
    total_saving = cost_comparison["total_cost_savings_percent"]
    critical_refutation = (
        chosen_metrics["critical_defects"] > baseline_metrics["critical_defects"]
    )
    quality_refutation = gap_low > float(settings["refutation_gap_pp"])
    cost_refutation = (
        total_saving is not None
        and total_saving < float(settings["refutation_savings_percent"])
    )
    if critical_refutation or quality_refutation or cost_refutation:
        report["hypothesis_status"] = "refuted"
        if critical_refutation:
            report["reasons"].append(
                "выбранный кандидат допустил дополнительный критический дефект"
            )
        if quality_refutation:
            report["reasons"].append(
                "доверительная нижняя граница отставания превысила порог опровержения"
            )
        if cost_refutation:
            report["reasons"].append("экономия полной стоимости ниже порога опровержения")
        return report
    confirmation = (
        sufficient
        and gap_high <= float(settings["quality_margin_pp"])
        and total_saving is not None
        and total_saving >= float(settings["confirmation_savings_percent"])
        and regression is not None
        and regression <= float(settings["max_review_regression_percent"])
        and chosen_metrics["critical_defects"] == 0
    )
    if confirmation:
        report["hypothesis_status"] = "confirmed"
        report["policy_action"] = "owner_decision_required"
        report["reasons"].append("все заранее заданные критерии подтверждения выполнены")
        return report
    if not sufficient:
        report["reasons"].append("объём отложенной выборки меньше заданного")
    if gap_high > float(settings["quality_margin_pp"]):
        report["reasons"].append("не доказано допустимое отставание по качеству")
    if total_saving is None:
        report["reasons"].append("полная стоимость не задана для всех настроечных запусков")
    elif total_saving < float(settings["confirmation_savings_percent"]):
        report["reasons"].append("экономия полной стоимости ниже порога подтверждения")
    if regression is None:
        report["reasons"].append("нельзя сопоставить время родительской проверки")
    elif regression > float(settings["max_review_regression_percent"]):
        report["reasons"].append("время родительской проверки ухудшилось сверх порога")
    return report


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Оценить последовательный выбор модели по готовой матрице запусков."
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error("вход и выход должны быть разными файлами")
    try:
        data, digest = load_input(args.input)
        report = evaluate(data, digest)
        write_json(args.output, report)
    except (InputError, OSError) as error:
        print(f"Ошибка оценки: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": report["hypothesis_status"],
                "selected_candidate": report["selected_candidate"],
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
