"""Builds the Onyx plugin zip with the deployment's address in the files, for a host
that takes no install options, such as claude.ai chat. `bundle/` is the copy of the
Claude files that `tools/agent_plugin/sync_bundle.py` writes."""

import io
import shlex
import zipfile
from pathlib import Path

from onyx.oauth_provider.models import OAuthProviderSettings
from onyx.server.features.onyx_plugin.models import (
    HooksFile,
    Marketplace,
    McpConfig,
    PluginManifest,
)

BUNDLE_DIR = Path(__file__).parent / "bundle"
# Read for the plugin version only: a marketplace file has no use in an upload.
MARKETPLACE_PATH = ".claude-plugin/marketplace.json"
MANIFEST_PATH = ".claude-plugin/plugin.json"
MCP_CONFIG_PATH = ".mcp.json"
HOOKS_PATH = "hooks/hooks.json"
MCP_SERVER_NAME = "onyx"
URL_OPTION = "onyx_url"
TOKEN_OPTION = "onyx_api_token"
TOKEN_NOTE = "Only needed for prefetch."


def _read(name: str) -> str:
    return (BUNDLE_DIR / name).read_text()


def _stamped_manifest() -> str:
    manifest = PluginManifest.model_validate_json(_read(MANIFEST_PATH))
    marketplace = Marketplace.model_validate_json(_read(MARKETPLACE_PATH))
    versions = {entry.name: entry.version for entry in marketplace.plugins}
    manifest.version = versions[manifest.name]
    del manifest.user_config[URL_OPTION]
    # The MCP server signs in through OAuth, so only the prefetch needs the token.
    token = manifest.user_config[TOKEN_OPTION]
    token.required = None
    token.description = f"{TOKEN_NOTE} {token.description}"
    return manifest.dump()


def _stamped_mcp_config(mcp_resource_url: str) -> str:
    config = McpConfig.model_validate_json(_read(MCP_CONFIG_PATH))
    server = config.mcp_servers[MCP_SERVER_NAME]
    server.url = mcp_resource_url
    server.headers = None
    return config.dump()


def _stamped_hooks(web_url: str) -> str:
    hooks = HooksFile.model_validate_json(_read(HOOKS_PATH))
    # The hook reads the bare ONYX_URL when the plugin has no URL option.
    prefix = f"ONYX_URL={shlex.quote(web_url)} "
    for groups in hooks.hooks.values():
        for group in groups:
            for hook in group.hooks:
                hook.command = prefix + hook.command
    return hooks.dump()


def build_plugin_bundle(settings: OAuthProviderSettings) -> bytes:
    stamped: dict[str, str] = {
        MANIFEST_PATH: _stamped_manifest(),
        MCP_CONFIG_PATH: _stamped_mcp_config(settings.mcp_resource_url),
        HOOKS_PATH: _stamped_hooks(settings.web_url),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(BUNDLE_DIR.rglob("*")):
            name = path.relative_to(BUNDLE_DIR).as_posix()
            if not path.is_file() or name == MARKETPLACE_PATH:
                continue
            # from_file keeps the launcher's executable bit.
            info = zipfile.ZipInfo.from_file(path, name)
            info.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(
                info, stamped[name] if name in stamped else path.read_bytes()
            )
    return buffer.getvalue()
