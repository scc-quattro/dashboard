"""Collect explicitly configured project reports without changing project files."""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "examples" / "dashboard.json"
PROJECT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
PHASES = ["代码与数据", "环境与小测", "完整基线", "优化与验证"]


def read_json(path):
    path = Path(path)
    if path.stat().st_size > 2_000_000:
        raise ValueError("JSON file exceeds 2 MB")
    return json.loads(path.read_text(encoding="utf-8"))


def relative_file(root, name):
    """Evidence and report paths must stay inside their configured project."""
    if not isinstance(name, str) or not name or Path(name).is_absolute():
        raise ValueError("Expected a relative project file")
    root = Path(root).resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Project file escapes its configured root")
    return path


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Expected a finite number")
    return value


def strings(value):
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("Expected a list of strings")
    return value


def stamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamps need a timezone")
    return parsed.isoformat()


def load_config(config_path=DEFAULT_CONFIG):
    config_path = Path(config_path).resolve()
    config = read_json(config_path)
    if not isinstance(config, dict) or config.get("schema_version") != 1:
        raise ValueError("Expected dashboard config schema_version 1")
    projects = config.get("projects")
    if not isinstance(projects, list) or not projects or len(projects) > 100:
        raise ValueError("Configure 1–100 projects")
    ids = set()
    for project in projects:
        if not isinstance(project, dict) or not PROJECT_ID.fullmatch(str(project.get("id", ""))):
            raise ValueError("Invalid project id")
        if project["id"] in ids:
            raise ValueError("Project ids must be unique")
        ids.add(project["id"])
        if not isinstance(project.get("path"), str) or not project["path"]:
            raise ValueError("Each project needs an explicit path")
        project["root"] = (config_path.parent / Path(project["path"]).expanduser()).resolve()
        relative_file(project["root"], project.get("report", "dashboard.json"))
        for key in ["name", "kind", "cluster", "description"]:
            if key in project and not isinstance(project[key], str):
                raise ValueError(f"Project {key} must be text")
    phases = strings(config.get("phase_labels", PHASES))
    if not 1 <= len(phases) <= 8:
        raise ValueError("Configure 1–8 phases")
    config["phase_labels"] = phases
    refresh = config.get("refresh_seconds", 60)
    if isinstance(refresh, bool) or not isinstance(refresh, int) or not 5 <= refresh <= 3600:
        raise ValueError("refresh_seconds must be 5–3600")
    config["refresh_seconds"] = refresh
    config["timezone"] = str(config.get("timezone", "Asia/Shanghai"))
    ZoneInfo(config["timezone"])
    traces = config.get("traces", {})
    if not isinstance(traces, dict):
        raise ValueError("traces must be an object")
    config["traces"] = traces
    config["config_dir"] = config_path.parent
    return config


def evidence_sources(root, records):
    if not isinstance(records, list) or len(records) > 100:
        raise ValueError("Expected up to 100 evidence references")
    result = []
    for record in records:
        path = relative_file(root, record["path"])
        if path.stat().st_size > 10_000_000:
            raise ValueError("Evidence files must be at most 10 MB")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        expected = record.get("sha256")
        if expected and expected != digest:
            raise ValueError("Evidence SHA256 mismatch")
        result.append({"path": record["path"], "label": str(record.get("label", path.name)),
                       "sha256": digest, "pinned": expected == digest,
                       "modified_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()})
    return result


def chart_report(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Invalid chart")
    labels = strings(value["labels"])
    series = value["series"]
    if not labels or len(labels) > 100 or not isinstance(series, list) or not 1 <= len(series) <= 8:
        raise ValueError("Charts need 1–100 labels and 1–8 series")
    clean_series = []
    for item in series:
        values = [number(v) for v in item["values"]]
        if len(values) != len(labels) or any(v < 0 for v in values):
            raise ValueError("Chart values must match labels and be nonnegative")
        clean_series.append({"name": str(item["name"]), "values": values})
    digits = value.get("digits", 2)
    if isinstance(digits, bool) or not isinstance(digits, int) or not 0 <= digits <= 6:
        raise ValueError("Chart digits must be 0–6")
    return {"title": str(value.get("title", "性能对比")), "unit": str(value.get("unit", "")),
            "direction": "higher" if value.get("direction") == "higher" else "lower",
            "insight": str(value.get("insight", "")), "note": str(value.get("note", "")),
            "digits": digits, "labels": labels, "series": clean_series}


def bounds_report(value):
    if value is None:
        return None
    result = {key: str(value.get(key, "")) for key in
              ["kind", "current", "headline", "ceiling", "gap", "meaning"]}
    result["status"] = value.get("status") if value.get("status") in {"conditional", "reference", "unknown"} else "unknown"
    for key in ["derivation", "assumptions", "next_measurements"]:
        result[key] = strings(value.get(key, []))
    result["sources"] = []
    result["references"] = []
    percent = value.get("model", {}).get("reference_percent")
    result["model"] = {"reference_percent": number(percent)} if percent is not None else {}
    return result


def learning_report(value):
    if not value or not value.get("versions"):
        return None
    if len(value["versions"]) > 100:
        raise ValueError("Too many versions")
    keys = ["version", "status", "title", "result", "before", "after", "bottleneck", "change", "validate", "lesson", "boundary", "source"]
    return {"versions": [{key: str(row.get(key, "")) for key in keys} for row in value["versions"]], "references": []}


def collect(config_path=DEFAULT_CONFIG):
    config = load_config(config_path)
    projects, warnings = [], []
    for item in config["projects"]:
        project = {"id": item["id"], "name": item.get("name", item["id"]),
                   "kind": item.get("kind", "CPU"), "cluster": item.get("cluster", "Local"),
                   "description": item.get("description", ""), "stage": "待核验", "tone": "neutral",
                   "summary": "尚无可读取的项目报告。", "baseline_verified": False,
                   "phase_states": ["pending"] * len(config["phase_labels"]),
                   "primary": {"text": "—", "unit": "", "label": "等待数据"},
                   "secondary": "", "correctness": "待验证", "next_steps": [], "limitations": [],
                   "jobs": [], "sources": [], "updated_at": None, "chart": None, "bounds": None,
                   "learning": None}
        try:
            report_path = relative_file(item["root"], item.get("report", "dashboard.json"))
            report = read_json(report_path)
            sources = evidence_sources(item["root"], report.get("sources", []))
            declared_verified = report.get("baseline_verified") is True
            verified = declared_verified and bool(sources) and all(source["pinned"] for source in sources)
            if declared_verified and not verified:
                raise ValueError("Verified reports need pinned evidence SHA256 references")
            states = strings(report.get("phase_states", project["phase_states"]))
            if len(states) != len(config["phase_labels"]) or not set(states) <= {"pending", "active", "done"}:
                raise ValueError("Phase states must match the configured phases")
            primary = report.get("primary", project["primary"])
            if "value" in primary:
                digits = primary.get("digits", 2)
                if isinstance(digits, bool) or not isinstance(digits, int) or not 0 <= digits <= 6:
                    raise ValueError("Metric digits must be 0–6")
                primary = {"value": number(primary["value"]), "digits": digits,
                           "unit": str(primary.get("unit", "")), "label": str(primary.get("label", ""))}
            else:
                primary = {key: str(primary.get(key, "")) for key in ["text", "unit", "label"]}
            jobs = []
            for job in report.get("jobs", []):
                jobs.append({"id": str(job["id"]), "project": item["id"], "label": str(job.get("label", "")),
                             "version": str(job.get("version", "")), "elapsed": str(job.get("elapsed", "")),
                             "state": job.get("state") if job.get("state") in {"RUNNING", "PENDING", "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL"} else "UNKNOWN",
                             "observed_at": stamp(job["observed_at"])})
            # Construct all fields before updating, so malformed reports cannot leave partial results.
            values = {"stage": str(report.get("stage", "待核验")),
                      "tone": report.get("tone") if report.get("tone") in {"neutral", "success", "warning"} else "neutral",
                      "summary": str(report.get("summary", "")), "baseline_verified": verified,
                      "phase_states": states, "primary": primary, "secondary": str(report.get("secondary", "")),
                      "correctness": str(report.get("correctness", "待验证")),
                      "next_steps": strings(report.get("next_steps", [])), "limitations": strings(report.get("limitations", [])),
                      "jobs": jobs, "sources": sources,
                      "updated_at": datetime.fromtimestamp(report_path.stat().st_mtime, timezone.utc).isoformat(),
                      "chart": chart_report(report.get("chart")) if verified else None,
                      "bounds": bounds_report(report.get("bounds")) if verified else None,
                      "learning": learning_report(report.get("learning"))}
            project.update(values)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as error:
            project.update(stage="记录待核验", tone="warning", summary="报告缺失、格式无效或证据校验失败。")
            warnings.append({"project": project["name"], "message": f"报告或证据未通过校验（{type(error).__name__}）"})
        projects.append(project)
    jobs = [job for project in projects for job in project["jobs"]]
    return {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
            "demo": config.get("demo") is True, "title": str(config.get("title", "Project Observatory")),
            "subtitle": str(config.get("subtitle", "只读项目看板")), "timezone": config["timezone"],
            "refresh_seconds": config["refresh_seconds"], "phase_labels": config["phase_labels"],
            "projects": projects, "jobs": jobs, "warnings": warnings,
            "summary": {"projects": len(projects), "cpu": sum(p["kind"] == "CPU" for p in projects),
                        "gpu": sum(p["kind"] == "GPU" for p in projects),
                        "verified_baselines": sum(p["baseline_verified"] for p in projects),
                        "observed_running_jobs": sum(job["state"] == "RUNNING" for job in jobs)}}
