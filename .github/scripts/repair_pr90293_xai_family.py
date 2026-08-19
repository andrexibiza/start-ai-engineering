from __future__ import annotations

import sys
from pathlib import Path
from textwrap import dedent

root = Path(sys.argv[1] if len(sys.argv) > 1 else "work")


def read(path: str) -> str:
    return (root / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    (root / path).write_text(text, encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    text = read(path)
    count = text.count(old)
    if count != 1:
        raise RuntimeError(
            f"{path}: expected one anchor, found {count}: {old[:180]!r}"
        )
    write(path, text.replace(old, new, 1))


scheduler = "cron/scheduler.py"
family_helper = dedent(
    '''
    # Provider pins protect backend service ownership, not the credential
    # transport used to reach that same backend. Collapse only explicitly
    # verified same-service variants; never infer compatibility from suffixes
    # or model names. xAI OAuth and xAI API-key routes share the xAI endpoint
    # and model namespace while differing only in authentication transport.
    _CRON_FALLBACK_PROVIDER_FAMILIES: dict[str, str] = {
        "xai": "xai",
        "xai-oauth": "xai",
    }


    def _cron_fallback_provider_family(provider: str) -> str:
        from hermes_cli.providers import normalize_provider

        canonical = normalize_provider(provider or "")
        return _CRON_FALLBACK_PROVIDER_FAMILIES.get(canonical, canonical)


    '''
)
replace_once(
    scheduler,
    "def _is_transient_provider_resolve_error(exc: BaseException) -> bool:\n",
    family_helper + "def _is_transient_provider_resolve_error(exc: BaseException) -> bool:\n",
)
replace_once(
    scheduler,
    "            pinned_provider = normalize_provider(\n"
    "                str(job.get(\"provider\") or \"\")\n"
    "            )\n"
    "            pinned_model = str(job.get(\"model\") or \"\").strip()\n",
    "            pinned_provider = normalize_provider(\n"
    "                str(job.get(\"provider\") or \"\")\n"
    "            )\n"
    "            pinned_provider_family = _cron_fallback_provider_family(\n"
    "                pinned_provider\n"
    "            )\n"
    "            pinned_model = str(job.get(\"model\") or \"\").strip()\n",
)
replace_once(
    scheduler,
    "                incompatible = (\n"
    "                    bool(pinned_provider)\n"
    "                    and fb_provider_canonical != pinned_provider\n"
    "                ) or (\n"
    "                    bool(pinned_model) and fb_model != pinned_model\n"
    "                )\n",
    "                incompatible = (\n"
    "                    bool(pinned_provider)\n"
    "                    and _cron_fallback_provider_family(\n"
    "                        fb_provider_canonical\n"
    "                    ) != pinned_provider_family\n"
    "                ) or (\n"
    "                    bool(pinned_model) and fb_model != pinned_model\n"
    "                )\n",
)

model_test = "tests/cron/test_model_pin_fallback_90089.py"
new_test = dedent(
    '''
        def test_xai_oauth_pin_allows_same_service_api_key_route(self, tmp_path):
            from hermes_cli.auth import AuthError

            job = _base_job(model="grok-4.5", provider="xai-oauth")
            success, _output, error, model_used, provider_used = _run_with_fallback(
                job,
                primary_provider="xai-oauth",
                primary_raises=AuthError("xai oauth token expired"),
                fallback_provider="xai",
                fallback_model="grok-4.5",
                tmp_path=tmp_path,
            )

            assert success is True, error
            assert model_used == "grok-4.5"
            assert provider_used == "xai"

    '''
)
replace_once(
    model_test,
    "    def test_incompatible_pinned_pair_fails_closed_on_network_error(\n",
    new_test + "    def test_incompatible_pinned_pair_fails_closed_on_network_error(\n",
)
