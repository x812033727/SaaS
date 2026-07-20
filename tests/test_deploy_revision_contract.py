"""Static contract connecting the deployed commit to the public health probe."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_deploy_pipeline_injects_full_git_sha_into_web_image():
    script = (ROOT / "docker" / "saas-deploy.sh").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert 'export SAAS_GIT_SHA="$TARGET"' in script
    assert "SAAS_GIT_SHA: ${SAAS_GIT_SHA:-unknown}" in compose
    assert "ARG SAAS_GIT_SHA=unknown" in dockerfile
    assert "SAAS_GIT_SHA=${SAAS_GIT_SHA}" in dockerfile
