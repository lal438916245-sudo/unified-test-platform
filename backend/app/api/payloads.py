"""Normalize resource payloads without changing create/update semantics."""

from copy import deepcopy

_ENVIRONMENT_DEFAULTS = {
    "name": None,
    "host": "",
    "port": 0,
    "kind": "http",
    "protocol": "http",
    "secret_ref": "",
    "secret_kind": "",
    "labels": [],
    "notes": "",
    "enabled": 1,
}
_EXECUTOR_DEFAULTS = {
    "name": None,
    "python_ref": "",
    "cwd_root": "",
    "allowed_runners": [],
    "max_concurrency": 1,
    "notes": "",
    "enabled": 1,
}
_ASSET_DEFAULTS = {
    "name": None,
    "kind": None,
    "dataset_ref": "",
    "tpl_ref": "",
    "scenes_ref": "",
    "ground_truth_ref": "",
    "airtest_ref": "",
    "scenario_ref": "",
    "notes": "",
    "enabled": 1,
}


def _payload(
    payload: dict,
    defaults: dict,
    partial: bool,
    integer_fields: tuple[str, ...],
    create_defaults=None,
) -> dict:
    out = {
        key: payload[key] if key in payload else deepcopy(default)
        for key, default in defaults.items()
        if key in payload or not partial
    }
    for key in integer_fields:
        if key in out and out[key] is not None:
            out[key] = int(out[key])
    if not partial:
        for key, value in (create_defaults or {}).items():
            out.setdefault(key, value)
    return {key: value for key, value in out.items() if value is not None}


def environment_payload(payload: dict, partial: bool = False) -> dict:
    return _payload(payload, _ENVIRONMENT_DEFAULTS, partial, ("port", "enabled"))


def executor_payload(payload: dict, partial: bool = False) -> dict:
    # Existing schemas require the legacy columns, even for controlled refs.
    return _payload(
        payload,
        _EXECUTOR_DEFAULTS,
        partial,
        ("max_concurrency", "enabled"),
        {"python_executable": "", "cwd": ""},
    )


def asset_payload(payload: dict, partial: bool = False) -> dict:
    return _payload(payload, _ASSET_DEFAULTS, partial, ("enabled",))
