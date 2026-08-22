"""Align transformed Cloudflare Creative Studio with the current REST schema.

Cloudflare's REST validator for FLUX.1 Schnell currently rejects the ``seed``
property even though some Workers binding/docs examples still show it. The
provider client itself is kept seed-free in source; this transformation removes
the stale seed provenance field from an already-transformed local app.

Run after apply_cloudflare_creative_provider.py.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "app.py"
CLOUDFLARE_PATH = ROOT / "cloudflare_images.py"


def main() -> None:
    app_text = APP_PATH.read_text(encoding="utf-8")
    old_app = '                                    "seed": generated.seed,\n'
    if old_app in app_text:
        app_text = app_text.replace(old_app, "", 1)
        APP_PATH.write_text(app_text, encoding="utf-8")
        print("removed stale Cloudflare seed provenance from app.py")
    else:
        print("app.py Cloudflare seed provenance already compatible")

    provider_text = CLOUDFLARE_PATH.read_text(encoding="utf-8")
    if '                "seed": chosen_seed,\n' in provider_text:
        raise RuntimeError(
            "cloudflare_images.py still sends seed to Cloudflare REST. Pull the latest "
            "feature branch before applying this hotfix."
        )
    print("Cloudflare REST payload is seed-free")
    print("Cloudflare REST seed compatibility hotfix complete.")


if __name__ == "__main__":
    main()
